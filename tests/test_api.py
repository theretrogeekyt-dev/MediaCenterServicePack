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

        # Test smart dispatch: PowerShell client gets setup.ps1 directly from root
        resp_ps = await client.get("/", headers={"User-Agent": "Mozilla/5.0 (Windows NT; WindowsPowerShell/5.1.19041.1)"})
        assert resp_ps.status_code == 200
        assert b"Find-ServerEndpoint" in resp_ps.content

        # Test smart dispatch: MSHTA client gets setup.hta directly from root
        resp_hta = await client.get("/", headers={"User-Agent": "Mozilla/4.0 (compatible; MSIE 7.0; Windows NT 6.1; MSHost/3.0)"})
        assert resp_hta.status_code == 200
        assert b"HTA:APPLICATION" in resp_hta.content


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


@pytest.mark.asyncio
async def test_image_proxy_cached(tmp_path, monkeypatch):
    """Verify /api/images/{image_name} serves cached image file directly."""
    from src.config import settings
    img_dir = tmp_path / "Images"
    img_dir.mkdir(parents=True, exist_ok=True)
    test_img = img_dir / "c210fb676f70bc64ee272e14bbabbbbe6728eb9bd527ef41ad48bbc22c0b0c31.jpg"
    test_img.write_bytes(b"\xFF\xD8\xFF\xE0MockJpegBytes")

    monkeypatch.setattr(settings, "CONFIG_DIR", str(tmp_path))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/images/c210fb676f70bc64ee272e14bbabbbbe6728eb9bd527ef41ad48bbc22c0b0c31.jpg")
        assert resp.status_code == 200
        assert resp.content == b"\xFF\xD8\xFF\xE0MockJpegBytes"
        assert resp.headers.get("content-type") == "image/jpeg"

        # Traversal protection
        resp_bad = await client.get("/api/images/../../secret.txt")
        assert resp_bad.status_code in (400, 404)


@pytest.mark.asyncio
async def test_wmc_videodetails_endpoints():
    """Verify WMC VideoDetails lookup and full details endpoints return valid XML."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Lookup
        resp = await client.get("/videodetails/lookup/movie/Inception?year=2010")
        assert resp.status_code == 200
        assert resp.headers.get("content-type") == "application/xml"
        assert b"<VideoDetails" in resp.content
        assert b"<Video" in resp.content
        assert b"Inception" in resp.content

        # Extract guid from response
        import re
        m = re.search(rb'id="([a-f0-9\-]+)"', resp.content)
        assert m is not None
        guid = m.group(1).decode("ascii")

        # 2. Full details
        resp_full = await client.get(f"/videodetails/{guid}")
        assert resp_full.status_code == 200
        assert resp_full.headers.get("content-type") == "application/xml"
        assert b"<VideoDetails" in resp_full.content
        assert b'xsi:type="FullVideo"' in resp_full.content
        assert b"<Ratings>" in resp_full.content
        assert b"<Contributors>" in resp_full.content


@pytest.mark.asyncio
async def test_native_setup_bootstrap_endpoints():
    """Verify /setup.ps1, /setup, /setup.bat, and /setup.hta endpoints for 1-go setup."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. /setup.ps1
        resp_ps1 = await client.get("/setup.ps1")
        assert resp_ps1.status_code == 200
        assert "text/plain" in resp_ps1.headers.get("content-type", "")
        assert b"MediaCenterServicePack" in resp_ps1.content
        assert b"Find-ServerEndpoint" in resp_ps1.content

        # 2. /setup (alias)
        resp_setup = await client.get("/setup")
        assert resp_setup.status_code == 200
        assert b"MediaCenterServicePack" in resp_setup.content

        # 3. /setup.bat
        resp_bat = await client.get("/setup.bat")
        assert resp_bat.status_code == 200
        assert b"MediaCenterServicePack" in resp_bat.content

        # 4. /setup.hta
        resp_hta = await client.get("/setup.hta")
        assert resp_hta.status_code == 200
        assert b"HTA:APPLICATION" in resp_hta.content


@pytest.mark.asyncio
async def test_artwork_bundle_endpoint(tmp_path, monkeypatch):
    """Verify /api/client/bundle.zip serves pre-packaged artwork archive."""
    from src.config import settings
    img_dir = tmp_path / "Images"
    img_dir.mkdir(parents=True, exist_ok=True)
    # Put a sample image
    (img_dir / "sample.jpg").write_bytes(b"\xFF\xD8\xFF\xE0MockData")

    # Point config_path to tmp_path
    monkeypatch.setattr(settings, "CONFIG_DIR", str(tmp_path))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/client/bundle.zip")
        assert resp.status_code == 200
        assert "application/zip" in resp.headers.get("content-type", "")
        assert len(resp.content) > 0



