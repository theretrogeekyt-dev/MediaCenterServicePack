import asyncio
import ipaddress
import logging
import socket
import struct
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from dnslib import A, DNSHeader, DNSRecord, QTYPE, RCODE, RR

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


def is_private_address(ip_str: str) -> bool:
    """Determine if an IP address belongs to RFC1918 private, loopback, or link-local ranges."""
    try:
        ip = ipaddress.ip_address(ip_str)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        return False


class DNSRateLimiter:
    """Per-IP sliding window rate limiter to mitigate DNS amplification and flood attacks."""

    def __init__(self, limit_per_sec: int = 50, window_sec: float = 1.0):
        self.limit = limit_per_sec
        self.window = window_sec
        self.counts: Dict[str, List[float]] = defaultdict(lambda: [0.0, 0.0])  # [count, window_start]
        self._last_cleanup = time.time()

    def is_allowed(self, client_ip: str) -> bool:
        if self.limit <= 0:
            return True
        now = time.time()
        if now - self._last_cleanup > 60.0:
            self._cleanup(now)
            self._last_cleanup = now

        entry = self.counts[client_ip]
        count, start = entry[0], entry[1]
        if now - start > self.window:
            self.counts[client_ip] = [1.0, now]
            return True
        if count < self.limit:
            entry[0] += 1.0
            return True
        return False

    def _cleanup(self, now: float):
        expired = [ip for ip, (_, start) in self.counts.items() if now - start > self.window * 2]
        for ip in expired:
            del self.counts[ip]

    def reset(self):
        self.counts.clear()


class DNSStats:
    is_running: bool = False
    listening_port: int = 53
    target_ip: str = "127.0.0.1"
    public_target_ip: Optional[str] = None
    queries_total: int = 0
    queries_intercepted: int = 0
    queries_forwarded: int = 0
    queries_refused: int = 0
    queries_rate_limited: int = 0
    rate_limit_per_sec: int = 50
    authoritative_only: bool = False
    allow_external_recursion: bool = False
    last_query_time: Optional[str] = None
    last_error: Optional[str] = None
    intercepted_domains: List[str] = DEFAULT_INTERCEPTED_DOMAINS

    @classmethod
    def to_dict(cls) -> dict:
        return {
            "is_running": cls.is_running,
            "listening_port": cls.listening_port,
            "target_ip": cls.target_ip,
            "public_target_ip": cls.public_target_ip,
            "queries_total": cls.queries_total,
            "queries_intercepted": cls.queries_intercepted,
            "queries_forwarded": cls.queries_forwarded,
            "queries_refused": cls.queries_refused,
            "queries_rate_limited": cls.queries_rate_limited,
            "rate_limit_per_sec": cls.rate_limit_per_sec,
            "authoritative_only": cls.authoritative_only,
            "allow_external_recursion": cls.allow_external_recursion,
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
        client_ip = addr[0]

        # 1. Rate Limiting (RRL) to mitigate DNS amplification and flood attacks
        if not self.service.rate_limiter.is_allowed(client_ip):
            DNSStats.queries_rate_limited += 1
            logger.debug(f"DNS rate limit exceeded for {client_ip}; dropping query.")
            return

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

            # Split-Horizon DNS Resolution:
            # - Private LAN clients receive local host IP to prevent hairpin NAT failure.
            # - Public WAN clients receive public WAN IP / DDNS so remote WMC connects successfully.
            is_client_private = is_private_address(client_ip)
            resolved_ip = DNSStats.target_ip if is_client_private else (DNSStats.public_target_ip or DNSStats.target_ip)

            if is_intercepted:
                # A or ANY query -> return our service IP
                if qtype_name in ("A", "ANY"):
                    reply = DNSRecord(DNSHeader(id=req.header.id, qr=1, aa=1, ra=1), q=req.q)
                    reply.add_answer(RR(req.q.qname, QTYPE.A, rdata=A(resolved_ip), ttl=60))
                    DNSStats.queries_intercepted += 1
                    logger.debug(f"Intercepted DNS query for {qname} (A) -> {resolved_ip} from {client_ip}")
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

            # Open Resolver & DDoS Protection:
            # 1. Authoritative-only mode rejects all non-intercepted domain queries
            if DNSStats.authoritative_only:
                reply = DNSRecord(DNSHeader(id=req.header.id, qr=1, aa=0, ra=0, rcode=RCODE.REFUSED), q=req.q)
                DNSStats.queries_refused += 1
                if self.transport:
                    self.transport.sendto(reply.pack(), addr)
                return

            # 2. If client is external (public WAN) and external recursion is NOT explicitly allowed, refuse to prevent DNS amplification
            if not is_client_private and not DNSStats.allow_external_recursion:
                reply = DNSRecord(DNSHeader(id=req.header.id, qr=1, aa=0, ra=0, rcode=RCODE.REFUSED), q=req.q)
                DNSStats.queries_refused += 1
                if self.transport:
                    self.transport.sendto(reply.pack(), addr)
                return

            # Forward external queries to upstream DNS (Cloudflare/Google)
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
        self.rate_limiter = DNSRateLimiter(limit_per_sec=getattr(settings, "DNS_RATE_LIMIT_PER_SEC", 50))

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
        peername = writer.get_extra_info("peername")
        client_ip = peername[0] if peername else "127.0.0.1"

        if not self.rate_limiter.is_allowed(client_ip):
            DNSStats.queries_rate_limited += 1
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return

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

            is_client_private = is_private_address(client_ip)
            resolved_ip = DNSStats.target_ip if is_client_private else (DNSStats.public_target_ip or DNSStats.target_ip)

            resp_bytes = None
            if is_intercepted and qtype_name in ("A", "ANY"):
                reply = DNSRecord(DNSHeader(id=req.header.id, qr=1, aa=1, ra=1), q=req.q)
                reply.add_answer(RR(req.q.qname, QTYPE.A, rdata=A(resolved_ip), ttl=60))
                resp_bytes = reply.pack()
                DNSStats.queries_intercepted += 1
            elif is_intercepted and qtype_name == "AAAA":
                reply = DNSRecord(DNSHeader(id=req.header.id, qr=1, aa=1, ra=1), q=req.q)
                resp_bytes = reply.pack()
                DNSStats.queries_intercepted += 1
            elif DNSStats.authoritative_only or (not is_client_private and not DNSStats.allow_external_recursion):
                reply = DNSRecord(DNSHeader(id=req.header.id, qr=1, aa=0, ra=0, rcode=RCODE.REFUSED), q=req.q)
                resp_bytes = reply.pack()
                DNSStats.queries_refused += 1
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
        public_target_ip = settings.public_host_ip

        DNSStats.listening_port = port
        DNSStats.target_ip = target_ip
        DNSStats.public_target_ip = public_target_ip
        DNSStats.rate_limit_per_sec = getattr(settings, "DNS_RATE_LIMIT_PER_SEC", 50)
        DNSStats.authoritative_only = getattr(settings, "DNS_AUTHORITATIVE_ONLY", False)
        DNSStats.allow_external_recursion = getattr(settings, "DNS_ALLOW_EXTERNAL_RECURSION", False)
        self.rate_limiter = DNSRateLimiter(limit_per_sec=DNSStats.rate_limit_per_sec)
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
                f"LAN target: {target_ip}, Public WAN target: {public_target_ip}. "
                f"DDoS Protection: RRL={DNSStats.rate_limit_per_sec} qps, Authoritative-Only={DNSStats.authoritative_only}, External-Recursion={DNSStats.allow_external_recursion}"
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

