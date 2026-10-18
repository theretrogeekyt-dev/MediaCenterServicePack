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
async def test_dashboard_html():
    """Verify HTML dashboard renders."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "MediaCenterServicePack" in resp.text
        assert "EPG Lineup Service" in resp.text
        assert "sync-wmc.ps1" in resp.text
