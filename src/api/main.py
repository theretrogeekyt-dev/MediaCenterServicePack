import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from src import __version__
from src.config import settings
from src.epg.pipeline import EPGPipeline, EPGStats
from src.metadata.movie_scanner import MovieScannerStats
from src.metadata.music_scanner import MusicScannerStats
from src.scheduler.tasks import scheduler_service

logger = logging.getLogger("openmce")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

START_TIME = time.time()
TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle management."""
    logger.info("Initializing Project eHome (OpenMCE Backend)...")

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
    logger.info("Shutting down Project eHome backend...")
    scheduler_service.shutdown()


app = FastAPI(
    title="Project eHome (OpenMCE Backend)",
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
        "movies": MovieScannerStats.to_dict(),
        "music": MusicScannerStats.to_dict(),
    }


@app.get("/api/status")
async def get_system_status():
    """Detailed system status, metrics, and scheduled tasks."""
    return {
        "version": __version__,
        "uptime_seconds": int(time.time() - START_TIME),
        "timezone": settings.TZ,
        "epg": EPGStats.to_dict(),
        "movies": MovieScannerStats.to_dict(),
        "music": MusicScannerStats.to_dict(),
        "jobs": scheduler_service.get_job_info(),
        "config": {
            "config_dir": str(settings.config_path),
            "movies_dir": str(settings.movies_path),
            "music_dir": str(settings.music_path),
            "xmltv_configured": bool(settings.XMLTV_URL or settings.XMLTV_FILE),
            "schedules_direct_configured": bool(settings.SCHEDULES_DIRECT_USER),
            "tmdb_configured": bool(settings.TMDB_API_KEY),
        },
    }


@app.get("/api/guide.mxf")
async def get_guide_mxf():
    """Serve the compiled Media Center XML Format (MXF) guide data."""
    mxf_file = settings.guide_mxf_file

    # If the file does not exist, trigger a build synchronously
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
async def trigger_epg_sync(source_url: Optional[str] = None):
    """Manually trigger an EPG synchronization pass."""
    try:
        pipeline = EPGPipeline()
        output_file = await pipeline.run(source_url=source_url)
        return {
            "status": "success",
            "message": f"EPG compiled successfully. Output written to {output_file.name}",
            "stats": EPGStats.to_dict(),
        }
    except Exception as e:
        logger.error(f"Manual EPG sync failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/media/scan")
async def trigger_media_scan(background_tasks: BackgroundTasks, force_refresh: bool = False):
    """Manually trigger background movie and music library scan."""
    background_tasks.add_task(scheduler_service.run_media_scan, force_refresh=force_refresh)
    return {
        "status": "accepted",
        "message": "Media scanning task dispatched to background worker.",
    }


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
            "movies": MovieScannerStats.to_dict(),
            "music": MusicScannerStats.to_dict(),
        },
    )
