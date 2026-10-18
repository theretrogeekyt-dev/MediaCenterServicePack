import asyncio
import logging
import socket
import struct
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from dnslib import A, DNSHeader, DNSRecord, QTYPE, RR

from src.config import settings

logger = logging.getLogger("mediacenter.dns")

DEFAULT_INTERCEPTED_DOMAINS = [
    # Auto-discovery hostnames
    "mediacenter.local",
    "openmce.local",
    "wmc.local",
    "epg.local",
    "servicepack.local",
    "mediacenter",
    "wmc",
    "openmce",
    # Legacy Windows Media Center TV Guide / EPG endpoints
    "data.tvdownload.microsoft.com",
    "services.tvdownload.microsoft.com",
    "mcepg.tvdownload.microsoft.com",
    "tvdownload.microsoft.com",
    "cdn.tvdownload.microsoft.com",
    # Legacy Windows Media Center Movie & DVD metadata
    "dvd.windowsmedia.com",
    "dvd.metaservices.microsoft.com",
    "metaservices.windowsmedia.com",
    # Legacy Windows Media Player / WMC CD artwork
    "fai.music.metaservices.microsoft.com",
    "redir.metaservices.microsoft.com",
]


class DNSStats:
    is_running: bool = False
    listening_port: int = 53
    target_ip: str = "127.0.0.1"
    queries_total: int = 0
    queries_intercepted: int = 0
    queries_forwarded: int = 0
    last_query_time: Optional[str] = None
    last_error: Optional[str] = None
    intercepted_domains: List[str] = DEFAULT_INTERCEPTED_DOMAINS

    @classmethod
    def to_dict(cls) -> dict:
        return {
            "is_running": cls.is_running,
            "listening_port": cls.listening_port,
            "target_ip": cls.target_ip,
            "queries_total": cls.queries_total,
            "queries_intercepted": cls.queries_intercepted,
            "queries_forwarded": cls.queries_forwarded,
            "last_query_time": cls.last_query_time,
            "last_error": cls.last_error,
            "intercepted_domains": cls.intercepted_domains,
        }


class DNSUDPProtocol(asyncio.DatagramProtocol):
    def __init__(self, service: "DNSService"):
        self.service = service
        self.transport: Optional[asyncio.DatagramTransport] = None

    def connection_made(self, transport: asyncio.DatagramTransport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr: Tuple[str, int]):
        asyncio.create_task(self.handle_query(data, addr))

    async def handle_query(self, data: bytes, addr: Tuple[str, int]):
        DNSStats.queries_total += 1
        DNSStats.last_query_time = datetime.now(timezone.utc).isoformat()
        try:
            req = DNSRecord.parse(data)
            qname = str(req.q.qname).rstrip(".").lower()
            qtype_id = req.q.qtype
            qtype_name = QTYPE.get(qtype_id, "")

            is_intercepted = any(
                qname == domain.lower() or qname.endswith("." + domain.lower())
                for domain in DNSStats.intercepted_domains
            )

            if is_intercepted:
                # A or ANY query -> return our service IP
                if qtype_name in ("A", "ANY"):
                    reply = DNSRecord(DNSHeader(id=req.header.id, qr=1, aa=1, ra=1), q=req.q)
                    reply.add_answer(RR(req.q.qname, QTYPE.A, rdata=A(DNSStats.target_ip), ttl=60))
                    DNSStats.queries_intercepted += 1
                    logger.debug(f"Intercepted DNS query for {qname} (A) -> {DNSStats.target_ip} from {addr[0]}")
                    if self.transport:
                        self.transport.sendto(reply.pack(), addr)
                    return
                # AAAA query -> return empty NOERROR so clients immediately fall back to IPv4
                elif qtype_name == "AAAA":
                    reply = DNSRecord(DNSHeader(id=req.header.id, qr=1, aa=1, ra=1), q=req.q)
                    DNSStats.queries_intercepted += 1
                    if self.transport:
                        self.transport.sendto(reply.pack(), addr)
                    return

            # Forward external queries to upstream DNS
            resp = await self.service.forward_query(data)
            if resp and self.transport:
                DNSStats.queries_forwarded += 1
                self.transport.sendto(resp, addr)

        except Exception as e:
            logger.debug(f"Error handling DNS datagram from {addr}: {e}")


class DNSService:
    """Manages the lightweight async DNS server for auto-discovery and WMC domain interception."""

    def __init__(self):
        self.udp_transport: Optional[asyncio.DatagramTransport] = None
        self.tcp_server: Optional[asyncio.Server] = None
        self.upstreams: List[Tuple[str, int]] = []

    def configure_upstreams(self):
        upstreams = []
        raw = getattr(settings, "UPSTREAM_DNS", "1.1.1.1,8.8.8.8")
        for server in raw.split(","):
            s = server.strip()
            if not s:
                continue
            if ":" in s:
                parts = s.split(":", 1)
                upstreams.append((parts[0].strip(), int(parts[1].strip())))
            else:
                upstreams.append((s, 53))
        self.upstreams = upstreams or [("1.1.1.1", 53), ("8.8.8.8", 53)]

    async def forward_query(self, data: bytes) -> bytes:
        """Forward a DNS query to configured upstream servers with timeout and fallback."""
        loop = asyncio.get_running_loop()

        def _sync_forward():
            for s_ip, s_port in self.upstreams:
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                        sock.settimeout(2.0)
                        sock.sendto(data, (s_ip, s_port))
                        resp, _ = sock.recvfrom(4096)
                        return resp
                except Exception:
                    continue
            return b""

        try:
            return await loop.run_in_executor(None, _sync_forward)
        except Exception:
            return b""

    async def handle_tcp_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handle optional TCP DNS query."""
        try:
            # First 2 bytes specify packet length in DNS over TCP
            length_bytes = await reader.readexactly(2)
            length = struct.unpack("!H", length_bytes)[0]
            data = await reader.readexactly(length)

            req = DNSRecord.parse(data)
            qname = str(req.q.qname).rstrip(".").lower()
            qtype_name = QTYPE.get(req.q.qtype, "")

            is_intercepted = any(
                qname == domain.lower() or qname.endswith("." + domain.lower())
                for domain in DNSStats.intercepted_domains
            )

            if is_intercepted and qtype_name in ("A", "ANY"):
                reply = DNSRecord(DNSHeader(id=req.header.id, qr=1, aa=1, ra=1), q=req.q)
                reply.add_answer(RR(req.q.qname, QTYPE.A, rdata=A(DNSStats.target_ip), ttl=60))
                resp_bytes = reply.pack()
                DNSStats.queries_intercepted += 1
            else:
                resp_bytes = await self.forward_query(data)
                if resp_bytes:
                    DNSStats.queries_forwarded += 1

            if resp_bytes:
                prefix = struct.pack("!H", len(resp_bytes))
                writer.write(prefix + resp_bytes)
                await writer.drain()

        except Exception as e:
            logger.debug(f"TCP DNS query handling error: {e}")
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def start(self):
        """Start UDP and TCP DNS servers."""
        if not getattr(settings, "ENABLE_DNS_SERVER", True):
            logger.info("DNS auto-discovery server is disabled in settings.")
            return

        port = getattr(settings, "DNS_PORT", 53)
        target_ip = settings.detected_host_ip
        DNSStats.listening_port = port
        DNSStats.target_ip = target_ip
        self.configure_upstreams()

        loop = asyncio.get_running_loop()

        # Start UDP listener
        try:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: DNSUDPProtocol(self),
                local_addr=("0.0.0.0", port),
            )
            self.udp_transport = transport
            DNSStats.is_running = True
            DNSStats.last_error = None
            logger.info(
                f"DNS auto-discovery server listening on 0.0.0.0:{port} (UDP). "
                f"WMC domains redirecting to: {target_ip}"
            )
        except OSError as e:
            DNSStats.is_running = False
            DNSStats.last_error = f"Cannot bind UDP port {port}: {e}"
            logger.warning(
                f"Could not bind DNS server to UDP port {port} ({e}). "
                f"If running non-root, ensure port 53 is exposed or set DNS_PORT in settings."
            )

        # Start TCP listener (best-effort)
        try:
            self.tcp_server = await asyncio.start_server(
                self.handle_tcp_client,
                "0.0.0.0",
                port,
            )
            logger.info(f"DNS auto-discovery server listening on 0.0.0.0:{port} (TCP).")
        except OSError as e:
            logger.debug(f"TCP DNS listener on port {port} not started: {e}")

    async def stop(self):
        """Shut down DNS listeners."""
        if self.udp_transport:
            self.udp_transport.close()
            self.udp_transport = None
        if self.tcp_server:
            self.tcp_server.close()
            try:
                await self.tcp_server.wait_closed()
            except Exception:
                pass
            self.tcp_server = None
        DNSStats.is_running = False
        logger.info("DNS auto-discovery server stopped.")


dns_service = DNSService()
