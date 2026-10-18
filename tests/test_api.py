from lxml import etree
import pytest
from httpx import ASGITransport, AsyncClient

from src.api.main import app


@pytest.mark.asyncio
async def test_health_endpoint():
    """Verify health check endpoint returns 200 and valid JSON status."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("healthy", "degraded")
        assert "version" in data
        assert "uptime_seconds" in data
        assert "epg" in data


@pytest.mark.asyncio
async def test_status_endpoint():
    """Verify detailed status endpoint."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "timezone" in data
        assert "config" in data
        assert "jobs" in data


@pytest.mark.asyncio
async def test_guide_mxf_serving():
    """Verify guide.mxf endpoint compiles and returns schema-compliant MXF XML."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/guide.mxf")
        assert resp.status_code == 200
        assert "application/xml" in resp.headers.get("content-type", "")

        # Verify XML payload
        root = etree.fromstring(resp.content)
        assert root.tag == "MXF"
        assert root.find("Assembly") is not None
        assert root.find("With") is not None


@pytest.mark.asyncio
async def test_manual_epg_sync():
    """Verify manual sync trigger endpoint."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/epg/sync")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"


@pytest.mark.asyncio
async def test_manual_epg_sync_with_api_key():
    """Verify manual sync trigger endpoint with direct service api_key."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/epg/sync?api_key=mock_service_key_123")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert "Source:" in data["message"]


@pytest.mark.asyncio
async def test_dashboard_html():
    """Verify HTML dashboard renders."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "MediaCenterServicePack" in resp.text
        assert "EPG Lineup Service" in resp.text
        assert "sync-wmc.ps1" in resp.text
        assert "TV Guide & Lineup" in resp.text


@pytest.mark.asyncio
async def test_epg_channels_lineup():
    """Verify GET /api/epg/channels returns formatted channel lineup and schedules."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/epg/channels")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_channels" in data
        assert data["total_channels"] > 0
        assert "channels" in data
        assert len(data["channels"]) > 0

        first_ch = data["channels"][0]
        assert "call_sign" in first_ch
        assert "number" in first_ch
        assert "upcoming" in first_ch


@pytest.mark.asyncio
async def test_epg_configure():
    """Verify POST /api/epg/configure updates settings and compiles EPG."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {
            "schedules_direct_user": "test_user_wmc",
            "postal_code": "67147",
        }
        resp = await client.post("/api/epg/configure", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("success", "warning")


@pytest.mark.asyncio
async def test_movie_dvdid_preview():
    """Verify GET /api/metadata/movies/{title}/dvdid returns raw XML for UI preview."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/metadata/movies/Inception/dvdid")
        assert resp.status_code == 200
        assert "application/xml" in resp.headers.get("content-type", "")
        assert b"<DvdId>" in resp.content or b"<MDR-DVD>" in resp.content


@pytest.mark.asyncio
async def test_client_setup_download():
    """Verify GET /api/client/setup.bat delivers configured batch file."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/client/setup.bat")
        assert resp.status_code == 200
        assert "setup-wmc.bat" in resp.headers.get("content-disposition", "")
        assert b"MediaCenterServicePack" in resp.content
        assert b"sync-wmc.ps1" in resp.content
        assert b"__SERVER_URL__" not in resp.content  # Verifies replacement happened


@pytest.mark.asyncio
async def test_client_sync_script_download():
    """Verify GET /api/client/sync-wmc.ps1 delivers PowerShell sync script."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/client/sync-wmc.ps1")
        assert resp.status_code == 200
        assert "sync-wmc.ps1" in resp.headers.get("content-disposition", "")
        assert b"MediaCenterServicePack" in resp.content
        assert b"loadmxf" in resp.content

