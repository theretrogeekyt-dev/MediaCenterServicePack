import asyncio
import struct
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from dnslib import DNSRecord, QTYPE
from httpx import ASGITransport, AsyncClient

from src.api.main import app
from src.config import settings
from src.dns.server import DNSService, DNSStats, DNSUDPProtocol, DNSRateLimiter, is_private_address


class MockDatagramTransport:
    def __init__(self):
        self.sent = []
        self.closed = False

    def sendto(self, data: bytes, addr):
        self.sent.append((data, addr))

    def close(self):
        self.closed = True


def test_dns_stats_to_dict():
    stats = DNSStats.to_dict()
    assert "is_running" in stats
    assert "listening_port" in stats
    assert "target_ip" in stats
    assert "queries_total" in stats
    assert "queries_intercepted" in stats
    assert "queries_forwarded" in stats
    assert "intercepted_domains" in stats
    assert "data.tvdownload.microsoft.com" in stats["intercepted_domains"]
    assert "mediacenter.local" in stats["intercepted_domains"]


def test_dns_configure_upstreams():
    service = DNSService()
    with patch.object(settings, "UPSTREAM_DNS", "1.1.1.1, 8.8.8.8:5353, 9.9.9.9"):
        service.configure_upstreams()
        assert ("1.1.1.1", 53) in service.upstreams
        assert ("8.8.8.8", 5353) in service.upstreams
        assert ("9.9.9.9", 53) in service.upstreams


@pytest.mark.asyncio
async def test_dns_udp_intercept_a_record():
    service = DNSService()
    DNSStats.target_ip = "192.168.1.100"
    init_intercepted = DNSStats.queries_intercepted

    protocol = DNSUDPProtocol(service)
    transport = MockDatagramTransport()
    protocol.connection_made(transport)

    # Query for legacy WMC TV guide endpoint
    query = DNSRecord.question("data.tvdownload.microsoft.com", "A")
    client_addr = ("192.168.1.50", 54321)

    await protocol.handle_query(query.pack(), client_addr)

    assert len(transport.sent) == 1
    resp_data, resp_addr = transport.sent[0]
    assert resp_addr == client_addr

    resp = DNSRecord.parse(resp_data)
    assert resp.header.qr == 1  # Response
    assert len(resp.rr) == 1
    assert str(resp.rr[0].rdata) == "192.168.1.100"
    assert DNSStats.queries_intercepted == init_intercepted + 1


@pytest.mark.asyncio
async def test_dns_udp_intercept_discovery_hostname():
    service = DNSService()
    DNSStats.target_ip = "10.0.0.5"

    protocol = DNSUDPProtocol(service)
    transport = MockDatagramTransport()
    protocol.connection_made(transport)

    # Query for local discovery name
    query = DNSRecord.question("mediacenter.local", "A")
    client_addr = ("10.0.0.20", 61234)

    await protocol.handle_query(query.pack(), client_addr)

    assert len(transport.sent) == 1
    resp_data, _ = transport.sent[0]
    resp = DNSRecord.parse(resp_data)
    assert len(resp.rr) == 1
    assert str(resp.rr[0].rdata) == "10.0.0.5"


@pytest.mark.asyncio
async def test_dns_udp_intercept_aaaa_record():
    service = DNSService()
    protocol = DNSUDPProtocol(service)
    transport = MockDatagramTransport()
    protocol.connection_made(transport)

    # Windows queries AAAA alongside A; service must return empty NOERROR
    query = DNSRecord.question("data.tvdownload.microsoft.com", "AAAA")
    client_addr = ("192.168.1.50", 54322)

    await protocol.handle_query(query.pack(), client_addr)

    assert len(transport.sent) == 1
    resp_data, _ = transport.sent[0]
    resp = DNSRecord.parse(resp_data)
    assert resp.header.qr == 1
    assert len(resp.rr) == 0  # No AAAA records returned, preventing IPv6 timeouts


@pytest.mark.asyncio
async def test_dns_udp_forward_external():
    service = DNSService()
    protocol = DNSUDPProtocol(service)
    transport = MockDatagramTransport()
    protocol.connection_made(transport)

    query = DNSRecord.question("example.com", "A")
    mock_reply = DNSRecord.question("example.com", "A")
    mock_reply.header.qr = 1

    with patch.object(service, "forward_query", new_callable=AsyncMock) as mock_forward:
        mock_forward.return_value = mock_reply.pack()
        init_forwarded = DNSStats.queries_forwarded

        await protocol.handle_query(query.pack(), ("192.168.1.50", 54323))

        mock_forward.assert_awaited_once()
        assert len(transport.sent) == 1
        assert DNSStats.queries_forwarded == init_forwarded + 1


@pytest.mark.asyncio
async def test_dns_tcp_client_handling():
    service = DNSService()
    DNSStats.target_ip = "192.168.1.200"

    query = DNSRecord.question("wmc.local", "A")
    query_bytes = query.pack()
    length_prefix = struct.pack("!H", len(query_bytes))

    reader = asyncio.StreamReader()
    reader.feed_data(length_prefix + query_bytes)
    reader.feed_eof()

    written_data = bytearray()
    writer = MagicMock()
    writer.write = MagicMock(side_effect=lambda b: written_data.extend(b))
    writer.drain = AsyncMock()
    writer.close = MagicMock()
    writer.wait_closed = AsyncMock()

    await service.handle_tcp_client(reader, writer)

    assert len(written_data) > 2
    resp_length = struct.unpack("!H", written_data[:2])[0]
    resp_bytes = bytes(written_data[2:2 + resp_length])
    resp = DNSRecord.parse(resp_bytes)
    assert len(resp.rr) == 1
    assert str(resp.rr[0].rdata) == "192.168.1.200"


@pytest.mark.asyncio
async def test_api_dns_endpoints():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # GET /api/dns/status
        resp = await client.get("/api/dns/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "target_ip" in data
        assert "intercepted_domains" in data

        # GET /health contains dns block
        resp = await client.get("/health")
        assert resp.status_code == 200
        health = resp.json()
        assert "dns" in health
        assert "queries_total" in health["dns"]

        # GET /api/status contains dns block & config
        resp = await client.get("/api/status")
        assert resp.status_code == 200
        status = resp.json()
        assert "dns" in status
        assert "dns_server_enabled" in status["config"]
        assert "dns_port" in status["config"]


@pytest.mark.asyncio
async def test_wmc_http_middleware_interception():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Native TV Guide request via intercepted DNS domain
        headers = {"Host": "data.tvdownload.microsoft.com"}
        resp = await client.get("/epg/tvguide", headers=headers)
        assert resp.status_code == 200
        assert "application/xml" in resp.headers.get("content-type", "")
        assert b"<MXF" in resp.content or b"<Assembly" in resp.content

        # 2. Normal host request does not intercept non-existent routes
        resp_normal = await client.get("/epg/tvguide", headers={"Host": "mediacenter-server:8080"})
        assert resp_normal.status_code == 404

        # 3. Native Movie & DVD metadata request via intercepted dvd.windowsmedia.com
        headers_movie = {"Host": "dvd.windowsmedia.com"}
        resp_movie = await client.get("/dvd/lookup?title=Inception", headers=headers_movie)
        assert resp_movie.status_code == 200
        assert "application/xml" in resp_movie.headers.get("content-type", "")
        assert b"<MDR-DVD>" in resp_movie.content or b"<DvdId>" in resp_movie.content

        # 4. Native Music album metadata request via intercepted fai.music.metaservices.microsoft.com
        headers_music = {"Host": "fai.music.metaservices.microsoft.com"}
        resp_music = await client.get("/cd/query?album=Thriller&artist=Michael+Jackson", headers=headers_music)
        assert resp_music.status_code == 200
        assert "application/json" in resp_music.headers.get("content-type", "")
        data = resp_music.json()
        assert "title" in data or "mbid" in data


def test_is_private_address():
    # RFC 1918 Private ranges
    assert is_private_address("10.0.0.1") is True
    assert is_private_address("172.16.0.5") is True
    assert is_private_address("192.168.1.100") is True
    # Loopback
    assert is_private_address("127.0.0.1") is True
    # Link local
    assert is_private_address("169.254.1.1") is True
    # Public WAN IPs
    assert is_private_address("1.1.1.1") is False
    assert is_private_address("8.8.8.8") is False
    assert is_private_address("73.189.44.22") is False
    # Invalid IP string
    assert is_private_address("not_an_ip") is False


def test_dns_rate_limiter():
    limiter = DNSRateLimiter(limit_per_sec=3, window_sec=1.0)
    client = "73.1.2.3"
    assert limiter.is_allowed(client) is True
    assert limiter.is_allowed(client) is True
    assert limiter.is_allowed(client) is True
    # Exceed limit
    assert limiter.is_allowed(client) is False
    # Different client is not impacted
    assert limiter.is_allowed("73.1.2.4") is True


@pytest.mark.asyncio
async def test_dns_udp_split_horizon_resolution():
    service = DNSService()
    DNSStats.target_ip = "192.168.1.100"       # Local LAN IP
    DNSStats.public_target_ip = "73.189.1.50"  # Public WAN IP
    protocol = DNSUDPProtocol(service)
    transport = MockDatagramTransport()
    protocol.connection_made(transport)

    query = DNSRecord.question("data.tvdownload.microsoft.com", "A")

    # 1. Local LAN client receives LAN IP
    local_client = ("192.168.1.55", 53001)
    await protocol.handle_query(query.pack(), local_client)
    resp = DNSRecord.parse(transport.sent[-1][0])
    assert str(resp.rr[0].rdata) == "192.168.1.100"

    # 2. Public WAN client receives Public WAN IP
    public_client = ("73.45.67.89", 53002)
    await protocol.handle_query(query.pack(), public_client)
    resp_pub = DNSRecord.parse(transport.sent[-1][0])
    assert str(resp_pub.rr[0].rdata) == "73.189.1.50"


@pytest.mark.asyncio
async def test_dns_udp_refuse_external_recursion():
    service = DNSService()
    DNSStats.authoritative_only = False
    DNSStats.allow_external_recursion = False
    protocol = DNSUDPProtocol(service)
    transport = MockDatagramTransport()
    protocol.connection_made(transport)

    query = DNSRecord.question("random-external-domain.com", "A")

    # Public WAN client trying to use server as open recursive resolver gets REFUSED
    public_client = ("93.184.216.34", 53003)
    await protocol.handle_query(query.pack(), public_client)
    assert len(transport.sent) == 1
    resp = DNSRecord.parse(transport.sent[-1][0])
    assert resp.header.rcode == 5  # RCODE.REFUSED


@pytest.mark.asyncio
async def test_dns_udp_authoritative_only_mode():
    service = DNSService()
    DNSStats.authoritative_only = True
    protocol = DNSUDPProtocol(service)
    transport = MockDatagramTransport()
    protocol.connection_made(transport)

    query = DNSRecord.question("google.com", "A")

    # Even a LAN client query for non-intercepted domain gets REFUSED in authoritative-only mode
    lan_client = ("10.0.0.15", 53004)
    await protocol.handle_query(query.pack(), lan_client)
    assert len(transport.sent) == 1
    resp = DNSRecord.parse(transport.sent[-1][0])
    assert resp.header.rcode == 5  # RCODE.REFUSED

    # Intercepted domain still resolves successfully
    wmc_query = DNSRecord.question("mediacenter.local", "A")
    await protocol.handle_query(wmc_query.pack(), lan_client)
    assert len(transport.sent) == 2
    wmc_resp = DNSRecord.parse(transport.sent[-1][0])
    assert wmc_resp.header.rcode == 0
    assert len(wmc_resp.rr) == 1


@pytest.mark.asyncio
async def test_dns_rate_limiting_drops_packet():
    service = DNSService()
    service.rate_limiter = DNSRateLimiter(limit_per_sec=2, window_sec=1.0)
    protocol = DNSUDPProtocol(service)
    transport = MockDatagramTransport()
    protocol.connection_made(transport)

    query = DNSRecord.question("mediacenter.local", "A")
    client = ("192.168.1.99", 53005)

    # 1st and 2nd query are allowed
    await protocol.handle_query(query.pack(), client)
    await protocol.handle_query(query.pack(), client)
    assert len(transport.sent) == 2

    # 3rd query exceeds limit -> dropped silently
    init_limited = DNSStats.queries_rate_limited
    await protocol.handle_query(query.pack(), client)
    assert len(transport.sent) == 2  # No new response sent
    assert DNSStats.queries_rate_limited == init_limited + 1


def test_public_host_ip_config():
    # If DNS_PUBLIC_IP is an explicit IP, returns it directly
    with patch.object(settings, "DNS_PUBLIC_IP", "73.189.50.25"):
        assert settings.public_host_ip == "73.189.50.25"

    # If DNS_PUBLIC_IP is a hostname, resolves it via socket
    with patch.object(settings, "DNS_PUBLIC_IP", "mywmc.synology.me"), \
         patch("socket.gethostbyname", return_value="73.189.50.99"):
        assert settings.public_host_ip == "73.189.50.99"


