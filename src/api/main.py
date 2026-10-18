import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from src import __version__
from src.config import settings
from src.epg.pipeline import EPGPipeline, EPGStats
from src.metadata.movie_engine import MovieEngine
from src.metadata.music_engine import MusicEngine
from src.scheduler.tasks import scheduler_service

logger = logging.getLogger("mediacenter")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

START_TIME = time.time()
TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Global Direct Metadata Engines (Direct online retrieval, zero media folder mounts)
movie_engine = MovieEngine()
music_engine = MusicEngine()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle management."""
    logger.info("Initializing MediaCenterServicePack...")

    # Start background scheduler
    scheduler_service.start()

    # Pre-compile guide.mxf if not present
    if not settings.guide_mxf_file.exists():
        logger.info("No guide.mxf found on boot. Compiling initial EPG dataset...")
        try:
            pipeline = EPGPipeline()
            await pipeline.run()
        except Exception as e:
            logger.error(f"Failed to generate initial guide.mxf: {e}")

    yield

    # Clean shutdown
    logger.info("Shutting down MediaCenterServicePack backend...")
    scheduler_service.shutdown()
    await movie_engine.close()
    await music_engine.close()


app = FastAPI(
    title="MediaCenterServicePack",
    description="EPG, Movie metadata, and Music artwork server for Windows Media Center",
    version=__version__,
    lifespan=lifespan,
)


@app.get("/health")
async def health_check():
    """Integrated health check endpoint for Docker / orchestration."""
    uptime = int(time.time() - START_TIME)
    is_healthy = EPGStats.status != "Error"
    return {
        "status": "healthy" if is_healthy else "degraded",
        "uptime_seconds": uptime,
        "version": __version__,
        "epg": EPGStats.to_dict(),
    }


@app.get("/api/status")
async def get_system_status():
    """Detailed system status, metrics, and scheduled tasks."""
    return {
        "version": __version__,
        "uptime_seconds": int(time.time() - START_TIME),
        "timezone": settings.TZ,
        "epg": EPGStats.to_dict(),
        "jobs": scheduler_service.get_job_info(),
        "config": {
            "config_dir": str(settings.config_path),
            "service_api_key_configured": bool(settings.epg_service_key),
            "schedules_direct_configured": bool(settings.SCHEDULES_DIRECT_USER or settings.epg_service_key),
            "xmltv_configured": bool(settings.XMLTV_URL or settings.XMLTV_FILE),
            "tmdb_configured": bool(settings.TMDB_API_KEY),
        },
    }


@app.get("/api/guide.mxf")
async def get_guide_mxf():
    """Serve the compiled Media Center XML Format (MXF) guide data."""
    mxf_file = settings.guide_mxf_file

    if not mxf_file.exists() or mxf_file.stat().st_size == 0:
        logger.info("guide.mxf requested but does not exist. Triggering compilation...")
        try:
            pipeline = EPGPipeline()
            await pipeline.run()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to generate guide.mxf: {e}")

    return FileResponse(
        path=str(mxf_file),
        media_type="application/xml",
        filename="guide.mxf",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Content-Disposition": 'attachment; filename="guide.mxf"',
        },
    )


@app.post("/api/epg/sync")
async def trigger_epg_sync(
    source_url: Optional[str] = None,
    api_key: Optional[str] = None,
):
    """Manually trigger an EPG synchronization pass, optionally supplying a direct service API key."""
    try:
        pipeline = EPGPipeline()
        output_file = await pipeline.run(source_url=source_url, api_key=api_key)
        return {
            "status": "success",
            "message": f"EPG compiled successfully. Source: {EPGStats.source}. Output written to {output_file.name}",
            "stats": EPGStats.to_dict(),
        }
    except Exception as e:
        logger.error(f"Manual EPG sync failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =========================================================================
# Direct Metadata API Endpoints (No Media Folders Required)
# =========================================================================

@app.get("/api/metadata/movies/search")
async def search_movies_direct(
    query: str = Query(..., description="Movie title to search on TMDb"),
    year: Optional[int] = Query(None, description="Optional 4-digit release year"),
    limit: int = Query(5, description="Maximum results to return"),
):
    """Directly query TMDb for movies without needing media files."""
    results = await movie_engine.search(query, year, limit=limit)
    return {"query": query, "count": len(results), "results": results}


@app.get("/api/metadata/movies/{movie_id}")
async def get_movie_metadata_direct(movie_id: int):
    """Directly fetch full movie metadata details from TMDb."""
    data = await movie_engine.get_movie_metadata(movie_id)
    if not data:
        raise HTTPException(status_code=404, detail=f"Movie ID {movie_id} not found.")
    return data


@app.get("/api/metadata/movies/{movie_id_or_title}/download")
async def download_movie_package_direct(
    movie_id_or_title: str,
    year: Optional[int] = Query(None, description="Optional release year"),
):
    """
    Directly download a complete WMC-compliant movie metadata bundle (.zip).
    Contains dvdid.xml (5.0 MDR-DVD schema), folder.jpg, and poster.jpg.
    No local media files or directories required!
    """
    pkg = await movie_engine.get_wmc_package(movie_id_or_title, year)
    if not pkg:
        raise HTTPException(status_code=404, detail="Movie metadata not found.")

    return Response(
        content=pkg["zip_package"],
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{pkg["filename"]}"',
        },
    )


@app.get("/api/metadata/music/search")
async def search_music_direct(
    query: str = Query(..., description="Album or release title to search on MusicBrainz"),
    artist: Optional[str] = Query(None, description="Optional artist name filter"),
    limit: int = Query(5, description="Maximum results to return"),
):
    """Directly query MusicBrainz for album releases without needing audio files."""
    results = await music_engine.search(query, artist, limit=limit)
    return {"query": query, "artist": artist, "count": len(results), "results": results}


@app.get("/api/metadata/music/{mbid}")
async def get_music_metadata_direct(mbid: str):
    """Directly fetch full album metadata and tracklist from MusicBrainz."""
    data = await music_engine.get_album_metadata(mbid)
    if not data:
        raise HTTPException(status_code=404, detail=f"MusicBrainz release {mbid} not found.")
    return data


@app.get("/api/metadata/music/{mbid_or_album}/download")
async def download_music_package_direct(
    mbid_or_album: str,
    artist: Optional[str] = Query(None, description="Optional artist name"),
):
    """
    Directly download a complete WMC-compliant music artwork bundle (.zip).
    Contains Folder.jpg (500x500 JPEG), AlbumArtSmall.jpg (75x75), and metadata.json.
    No local audio files or directories required!
    """
    pkg = await music_engine.get_wmc_package(mbid_or_album, artist)
    if not pkg:
        raise HTTPException(status_code=404, detail="Music metadata or artwork not found.")

    return Response(
        content=pkg["zip_package"],
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{pkg["filename"]}"',
        },
    )


@app.get("/", response_class=HTMLResponse)
async def index_page(request: Request):
    """Serve the management web dashboard."""
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "version": __version__,
            "tz": settings.TZ,
            "host_ip": request.client.host if request.client else "localhost",
            "port": settings.PORT,
            "epg": EPGStats.to_dict(),
        },
    )
