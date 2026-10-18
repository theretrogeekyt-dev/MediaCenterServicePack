import io
import zipfile
from lxml import etree
from PIL import Image
import pytest
from httpx import ASGITransport, AsyncClient

from src.api.main import app
from src.metadata.movie_engine import MovieEngine
from src.metadata.music_engine import MusicEngine


@pytest.mark.asyncio
async def test_direct_movie_engine():
    """Verify MovieEngine directly queries TMDb and generates WMC packages without media folders."""
    engine = MovieEngine()
    try:
        # 1. Search movies directly
        results = await engine.search("Inception", year=2010)
        assert len(results) > 0
        first = results[0]
        assert "Inception" in first["title"]

        # 2. Compile complete WMC package
        pkg = await engine.get_wmc_package(str(first["id"]))
        assert pkg is not None
        assert "dvdid_xml" in pkg
        assert "artwork" in pkg
        assert "zip_package" in pkg

        # Verify XML structure
        root = etree.fromstring(pkg["dvdid_xml"])
        assert root.tag == "METADATA"
        assert root.find(".//dvdTitle").text == first["title"]
        assert root.find("DvdId") is not None

        # Verify ZIP package contents
        with zipfile.ZipFile(io.BytesIO(pkg["zip_package"])) as zf:
            namelist = zf.namelist()
            assert "dvdid.xml" in namelist
            assert "folder.jpg" in namelist
            assert "poster.jpg" in namelist
            assert f"{pkg['dvd_id']}.xml" in namelist
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_direct_music_engine():
    """Verify MusicEngine directly queries MusicBrainz and generates 500x500 WMC packages without media folders."""
    engine = MusicEngine()
    try:
        # 1. Compile complete WMC music package
        pkg = await engine.get_wmc_package("Dark Side of the Moon", artist="Pink Floyd")
        assert pkg is not None
        assert "folder_jpg" in pkg
        assert "small_jpg" in pkg
        assert "zip_package" in pkg

        # Verify artwork dimensions
        with Image.open(io.BytesIO(pkg["folder_jpg"])) as img_500:
            assert img_500.size == (500, 500)
        with Image.open(io.BytesIO(pkg["small_jpg"])) as img_75:
            assert img_75.size == (75, 75)

        # Verify ZIP package contents
        with zipfile.ZipFile(io.BytesIO(pkg["zip_package"])) as zf:
            namelist = zf.namelist()
            assert "Folder.jpg" in namelist
            assert "AlbumArtSmall.jpg" in namelist
            assert "metadata.json" in namelist
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_direct_metadata_api_endpoints():
    """Verify FastAPI direct search and download endpoints."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Movie Search
        resp = await client.get("/api/metadata/movies/search?query=Casablanca")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] > 0
        movie_id = data["results"][0]["id"]

        # 2. Movie Details
        details_resp = await client.get(f"/api/metadata/movies/{movie_id}")
        assert details_resp.status_code == 200
        assert "title" in details_resp.json()

        # 3. Direct Movie ZIP Download
        dl_resp = await client.get(f"/api/metadata/movies/{movie_id}/download")
        assert dl_resp.status_code == 200
        assert dl_resp.headers["content-type"] == "application/zip"
        assert len(dl_resp.content) > 100

        # Verify ZIP can be opened and contains dvdid.xml
        with zipfile.ZipFile(io.BytesIO(dl_resp.content)) as zf:
            assert "dvdid.xml" in zf.namelist()

        # 4. Music Search
        music_resp = await client.get("/api/metadata/music/search?query=Abbey+Road")
        assert music_resp.status_code == 200

        # 5. Direct Music ZIP Download
        music_dl = await client.get("/api/metadata/music/Abbey%20Road/download?artist=The%20Beatles")
        assert music_dl.status_code == 200
        assert music_dl.headers["content-type"] == "application/zip"
        with zipfile.ZipFile(io.BytesIO(music_dl.content)) as zf:
            assert "Folder.jpg" in zf.namelist()
            assert "AlbumArtSmall.jpg" in zf.namelist()
