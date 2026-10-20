import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
import httpx
from pydantic import BaseModel

from src import __version__
from src.config import settings
from src.dns import dns_service, DNSStats
from src.epg.pipeline import EPGPipeline, EPGStats, generate_fallback_epg
from src.metadata.dvdid_generator import generate_dvd_id
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

port_80_server: Optional[asyncio.Server] = None


async def start_port_80_forwarder(target_port: int = 8080) -> Optional[asyncio.Server]:
    """
    Transparently forwards native port 80 requests to FastAPI on target_port.
    This enables WMC machines to query http://data.tvdownload.microsoft.com natively on port 80.
    """
    if not getattr(settings, "ENABLE_PORT_80_FORWARDER", True):
        return None
    if getattr(settings, "PORT", 8080) == 80:
        return None

    async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            target_reader, target_writer = await asyncio.open_connection("127.0.0.1", target_port)

            async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter):
                try:
                    while True:
                        buf = await src.read(4096)
                        if not buf:
                            break
                        dst.write(buf)
                        await dst.drain()
                except Exception:
                    pass
                finally:
                    try:
                        dst.close()
                    except Exception:
                        pass

            await asyncio.gather(
                pipe(reader, target_writer),
                pipe(target_reader, writer),
            )
        except Exception:
            try:
                writer.close()
            except Exception:
                pass

    try:
        server = await asyncio.start_server(handle_client, "0.0.0.0", 80)
        logger.info("Native WMC HTTP listener successfully bound to port 80 (forwarding to 8080).")
        return server
    except OSError as e:
        logger.info(f"Port 80 not available ({e}). Native WMC port 80 interception requires port mapping to 8080 or Tailscale.")
        return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle management."""
    logger.info("Initializing MediaCenterServicePack...")

    # Start background scheduler
    scheduler_service.start()

    # Start DNS auto-discovery & domain redirection server
    await dns_service.start()

    # Start optional native port 80 forwarder (auto-binds when port 80 is available)
    global port_80_server
    port_80_server = await start_port_80_forwarder(settings.PORT)

    # Pre-compile guide.mxf if not present
    existing_guide = settings.get_existing_guide_mxf()
    if not existing_guide:
        logger.info("No guide.mxf found on boot. Compiling initial EPG dataset...")
        try:
            pipeline = EPGPipeline()
            await pipeline.run()
        except Exception as e:
            logger.error(f"Failed to generate initial guide.mxf: {e}")

    yield

    # Clean shutdown
    logger.info("Shutting down MediaCenterServicePack backend...")
    if port_80_server:
        port_80_server.close()
        try:
            await port_80_server.wait_closed()
        except Exception:
            pass
        port_80_server = None
    await dns_service.stop()
    scheduler_service.shutdown()
    await movie_engine.close()
    await music_engine.close()


app = FastAPI(
    title="MediaCenterServicePack",
    description="EPG, Movie metadata, and Music artwork server for Windows Media Center",
    version=__version__,
    lifespan=lifespan,
)


@app.middleware("http")
async def wmc_domain_interception_middleware(request: Request, call_next):
    """
    Seamlessly intercepts native WMC HTTP requests redirected via DNS.
    Serves guide.mxf directly when clients query legacy Microsoft EPG domains.
    """
    host = request.headers.get("host", "").lower().split(":")[0]
    path = request.url.path

    # 1. Intercept native TV Guide download calls to *.tvdownload.microsoft.com
    if "tvdownload.microsoft.com" in host and not path.startswith("/api/") and not path.startswith("/health") and not path.startswith("/setup"):
        accept = request.headers.get("accept", "").lower()
        ua = request.headers.get("user-agent", "").lower()
        if path == "/" and "text/html" in accept and ("mozilla" in ua or "trident" in ua or "edge" in ua):
            return await call_next(request)

        logger.info(f"Intercepted native WMC guide request from host '{host}' at '{path}'. Serving guide.mxf...")
        mxf_file = settings.get_existing_guide_mxf()
        if mxf_file and mxf_file.exists():
            return FileResponse(
                path=str(mxf_file),
                media_type="application/xml",
                filename="guide.mxf",
                headers={"Content-Disposition": 'attachment; filename="guide.mxf"'},
            )
        elif EPGPipeline.last_mxf_bytes:
            return Response(
                content=EPGPipeline.last_mxf_bytes,
                media_type="application/xml",
                headers={"Content-Disposition": 'attachment; filename="guide.mxf"'},
            )

    # 2. Intercept native Movie & DVD metadata calls (dvd.windowsmedia.com, metaservices.windowsmedia.com)
    if ("windowsmedia.com" in host or "metaservices" in host) and ("dvd" in host or "dvd" in path):
        query = request.query_params.get("title") or request.query_params.get("query") or request.query_params.get("dvdid") or "Movie"
        logger.info(f"Intercepted native WMC Movie/DVD metadata query for '{query}' from host '{host}'")
        try:
            results = await movie_engine.search(query=query, limit=1)
            if results:
                dvd_xml = await movie_engine.get_dvdid_xml(results[0]["id"])
            else:
                dvd_xml = movie_engine.generator.build_xml({
                    "title": query,
                    "dvd_id": generate_dvd_id(query, ""),
                    "synopsis": f"Title: {query}",
                })
            if dvd_xml:
                return Response(content=dvd_xml, media_type="application/xml")
        except Exception as e:
            logger.debug(f"Error serving intercepted DVD metadata: {e}")

    # 3. Intercept native Music CD / Album info calls (fai.music.metaservices.microsoft.com)
    if "music.metaservices" in host or ("metaservices" in host and "music" in (host + path)):
        query = request.query_params.get("album") or request.query_params.get("query") or request.query_params.get("toc") or "Album"
        artist = request.query_params.get("artist")
        logger.info(f"Intercepted native WMC Music metadata query for '{query}' from host '{host}'")
        try:
            results = await music_engine.search(query=query, artist=artist, limit=1)
            if results:
                details = await music_engine.get_album_metadata(results[0]["mbid"])
            else:
                details = {
                    "title": query,
                    "artist": artist or "Unknown Artist",
                    "tracks": [],
                }
            if details:
                return JSONResponse(content=details)
        except Exception as e:
            logger.debug(f"Error serving intercepted Music metadata: {e}")

    return await call_next(request)


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
        "dns": DNSStats.to_dict(),
    }


@app.get("/api/status")
async def get_system_status():
    """Detailed system status, metrics, and scheduled tasks."""
    return {
        "version": __version__,
        "uptime_seconds": int(time.time() - START_TIME),
        "timezone": settings.TZ,
        "epg": EPGStats.to_dict(),
        "dns": DNSStats.to_dict(),
        "jobs": scheduler_service.get_job_info(),
        "config": {
            "config_dir": str(settings.config_path),
            "service_api_key_configured": bool(settings.epg_service_key),
            "schedules_direct_configured": bool(settings.SCHEDULES_DIRECT_USER or settings.epg_service_key),
            "xmltv_configured": bool(settings.XMLTV_URL or settings.XMLTV_FILE),
            "tmdb_configured": bool(settings.TMDB_API_KEY),
            "dns_server_enabled": settings.ENABLE_DNS_SERVER,
            "dns_port": settings.DNS_PORT,
            "detected_host_ip": settings.detected_host_ip,
        },
    }


@app.get("/api/dns/status")
async def get_dns_status():
    """Return DNS auto-discovery status and query statistics."""
    return DNSStats.to_dict()



def get_client_file(filename: str) -> Optional[Path]:
    candidates = [
        Path(__file__).resolve().parent.parent.parent / "client" / filename,
        Path("/app/client") / filename,
        Path("client") / filename,
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


@app.get("/setup.ps1")
@app.get("/setup")
async def get_setup_powershell_script(request: Request):
    """
    Serve the 1-go native PowerShell setup script.
    Allows setup via: powershell -nop -c "iex(New-Object Net.WebClient).DownloadString('http://mediacenter.local/setup.ps1')"
    """
    ps1_file = get_client_file("setup-wmc.ps1")
    if not ps1_file:
        raise HTTPException(status_code=404, detail="setup-wmc.ps1 not found.")
    content = ps1_file.read_text(encoding="utf-8", errors="ignore")
    server_ip = settings.detected_host_ip or request.url.hostname or "localhost"
    server_port = settings.PORT
    server_url = f"http://{server_ip}:{server_port}"
    content = content.replace("__SERVER_URL__", server_url)
    return Response(
        content=content.encode("utf-8"),
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/setup.bat")
async def get_setup_bat_direct(request: Request):
    """Serve the 1-click batch setup script directly from root URL."""
    return await download_setup_bat(request)


@app.get("/setup.hta")
@app.get("/api/client/setup-wmc.hta")
async def get_setup_hta(request: Request):
    """
    Serve the Microsoft HTML Application (HTA) for native GUI setup without external tools.
    Allows setup via: mshta http://mediacenter.local/setup.hta
    """
    hta_file = get_client_file("setup-wmc.hta")
    if not hta_file:
        raise HTTPException(status_code=404, detail="setup-wmc.hta not found.")
    content = hta_file.read_text(encoding="utf-8", errors="ignore")
    server_ip = settings.detected_host_ip or request.url.hostname or "localhost"
    server_port = settings.PORT
    server_url = f"http://{server_ip}:{server_port}"
    content = content.replace("http://mediacenter.local:8080", server_url)
    return Response(
        content=content.encode("utf-8"),
        media_type="application/hta",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/api/client/bundle.zip")
async def download_artwork_bundle():
    """
    Fast pre-packaged artwork archive bundle.
    Contains all guide posters, TV series artwork, movie covers, and channel logos.
    Allows a new Media Center PC to extract all artwork in 1-2 seconds.
    """
    bundle_path = settings.config_path / "images_bundle.zip"
    alt_bundle = Path("C:/ProgramData/MediaCenterServicePack/images_bundle.zip")
    images_dir = settings.config_path / "Images"
    alt_images = Path("C:/ProgramData/MediaCenterServicePack/Images")

    img_count = len(list(images_dir.iterdir())) if images_dir.exists() else 0
    alt_count = len(list(alt_images.iterdir())) if alt_images.exists() else 0

    target_dir = alt_images if alt_count > img_count else (images_dir if img_count > 0 else None)

    # Check if bundle already exists on disk
    has_bundle = bundle_path.exists() and bundle_path.stat().st_size > 0
    has_alt_bundle = alt_bundle.exists() and alt_bundle.stat().st_size > 0

    if not has_bundle and has_alt_bundle:
        bundle_path = alt_bundle
        has_bundle = True
    elif not has_bundle and not has_alt_bundle and target_dir:
        import zipfile
        save_path = bundle_path if bundle_path.parent.exists() else (alt_bundle if alt_bundle.parent.exists() else bundle_path)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(save_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
            for f in target_dir.iterdir():
                if f.is_file():
                    zf.write(f, arcname=f.name)
        bundle_path = save_path
        has_bundle = bundle_path.exists() and bundle_path.stat().st_size > 0

    if has_bundle:
        return FileResponse(
            path=str(bundle_path),
            media_type="application/zip",
            filename="images_bundle.zip",
            headers={"Content-Disposition": 'attachment; filename="images_bundle.zip"'},
        )
    raise HTTPException(status_code=404, detail="Images bundle not available.")


@app.get("/api/client/setup.bat")
async def download_setup_bat(request: Request):
    """
    Download pre-configured 1-click Windows setup batch script.
    Substitutes the detected server host IP so client auto-discovers immediately.
    """
    bat_file = get_client_file("setup-wmc.bat")
    if not bat_file:
        raise HTTPException(status_code=404, detail="setup-wmc.bat not found.")

    content = bat_file.read_text(encoding="utf-8", errors="ignore")
    server_ip = settings.detected_host_ip or request.url.hostname or "localhost"
    server_port = settings.PORT
    server_url = f"http://{server_ip}:{server_port}"
    content = content.replace("__SERVER_URL__", server_url)

    return Response(
        content=content.encode("utf-8"),
        media_type="application/x-bat",
        headers={
            "Content-Disposition": 'attachment; filename="setup-wmc.bat"',
            "Cache-Control": "no-cache",
        },
    )


@app.get("/api/client/sync-wmc.ps1")
async def download_sync_script():
    """Download the client PowerShell automation script."""
    ps1_file = get_client_file("sync-wmc.ps1")
    if not ps1_file:
        raise HTTPException(status_code=404, detail="sync-wmc.ps1 not found.")
    return FileResponse(
        path=str(ps1_file),
        media_type="text/plain",
        filename="sync-wmc.ps1",
        headers={
            "Content-Disposition": 'attachment; filename="sync-wmc.ps1"',
            "Cache-Control": "no-cache",
        },
    )


@app.get("/api/guide.mxf")
async def get_guide_mxf():
    """Serve the compiled Media Center XML Format (MXF) guide data."""
    mxf_file = settings.get_existing_guide_mxf()

    if not mxf_file:
        logger.info("guide.mxf requested but does not exist. Triggering compilation...")
        try:
            pipeline = EPGPipeline()
            mxf_file = await pipeline.run()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to generate guide.mxf: {e}")

    if mxf_file and mxf_file.exists():
        return FileResponse(
            path=str(mxf_file),
            media_type="application/xml",
            filename="guide.mxf",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Content-Disposition": 'attachment; filename="guide.mxf"',
            },
        )
    elif EPGPipeline.last_mxf_bytes:
        return Response(
            content=EPGPipeline.last_mxf_bytes,
            media_type="application/xml",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Content-Disposition": 'attachment; filename="guide.mxf"',
            },
        )
    else:
        raise HTTPException(status_code=500, detail="Failed to locate or generate guide.mxf payload.")


@app.get("/api/images/{image_name}")
async def get_cached_image(image_name: str):
    """
    Serve cached guide artwork and movie posters directly to Windows Media Center.
    Falls back to fetching from Schedules Direct dynamically if not yet cached locally.
    """
    clean_name = Path(image_name).name
    if not clean_name or clean_name != image_name or "/" in image_name or "\\" in image_name:
        raise HTTPException(status_code=400, detail="Invalid image file name.")

    # 1. Check local images cache directory
    candidates = [
        settings.config_path / "Images" / clean_name,
        Path("C:/ProgramData/MediaCenterServicePack/Images") / clean_name,
        Path("/config/Images") / clean_name,
    ]
    for cand in candidates:
        if cand.exists() and cand.is_file():
            return FileResponse(
                path=str(cand),
                media_type="image/jpeg",
                headers={"Cache-Control": "public, max-age=2592000, immutable"},
            )

    # 2. If it is an SD image hash, attempt dynamic download & cache
    if clean_name.endswith(".jpg") and len(clean_name) == 68:  # 64 hex chars + .jpg
        from src.epg.schedules_direct import SchedulesDirectClient
        sd_user = settings.SCHEDULES_DIRECT_USER
        sd_pass = settings.SCHEDULES_DIRECT_PASS
        sd_key = settings.epg_service_key

        sd = SchedulesDirectClient(
            token=sd_key,
            username=sd_user,
            password=sd_pass,
            base_url=settings.EPG_SERVICE_URL,
        )
        token = await sd.login()
        await sd.close()

        if token:
            try:
                async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                    resp = await client.get(
                        f"https://json.schedulesdirect.org/20141201/image/{clean_name}",
                        headers={"token": token, "User-Agent": "MediaCenterServicePack/1.0"},
                    )
                    if resp.status_code == 200 and len(resp.content) > 500:
                        save_target = candidates[0]
                        save_target.parent.mkdir(parents=True, exist_ok=True)
                        save_target.write_bytes(resp.content)
                        return Response(
                            content=resp.content,
                            media_type="image/jpeg",
                            headers={"Cache-Control": "public, max-age=2592000, immutable"},
                        )
            except Exception as e:
                logger.warning(f"Dynamic image fetch failed for {clean_name}: {e}")

    raise HTTPException(status_code=404, detail="Image not found.")


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


class ConfigureEPGRequest(BaseModel):
    schedules_direct_user: Optional[str] = None
    schedules_direct_pass: Optional[str] = None
    postal_code: Optional[str] = None
    epg_api_key: Optional[str] = None
    tmdb_api_key: Optional[str] = None


@app.get("/api/epg/channels")
async def get_epg_channels():
    """Return active channel lineup and upcoming schedule for browser viewing."""
    db = EPGPipeline.last_database
    if not db or not db.channels:
        # Load or generate fallback if not yet loaded in memory
        try:
            pipeline = EPGPipeline()
            await pipeline.run()
            db = EPGPipeline.last_database
        except Exception:
            pass

    if not db or not db.channels:
        db = generate_fallback_epg()
        EPGPipeline.last_database = db

    now = datetime.now(timezone.utc)
    channels_list = []

    # Map schedule entries by service_id
    schedule_by_service: Dict[str, list] = {}
    for entry in db.schedule_entries:
        schedule_by_service.setdefault(entry.service_id, []).append(entry)

    # Sort channels by number and sub_number
    sorted_channels = sorted(
        db.channels.values(),
        key=lambda c: (c.number or 9999, c.sub_number or 0, c.call_sign or ""),
    )

    for ch in sorted_channels:
        ch_num_str = f"{ch.number}.{ch.sub_number}" if ch.sub_number else str(ch.number or "")
        
        # Get upcoming programs
        entries = schedule_by_service.get(ch.service_id, [])
        entries_sorted = sorted(entries, key=lambda e: e.start_time)
        
        upcoming = []
        for e in entries_sorted:
            end_t = e.start_time + timedelta(seconds=e.duration_seconds)
            if end_t >= now - timedelta(minutes=15):
                prog = db.programs.get(e.program_id)
                upcoming.append({
                    "title": prog.title if prog else "Scheduled Broadcast",
                    "episode_title": prog.episode_title if prog else None,
                    "description": prog.description if prog else None,
                    "start_time": e.start_time.isoformat(),
                    "start_time_display": e.start_time.strftime("%I:%M %p"),
                    "duration_mins": int(e.duration_seconds / 60),
                    "is_movie": prog.is_movie if prog else False,
                    "is_series": prog.is_series if prog else False,
                    "mpaa_rating": prog.mpaa_rating if prog else None,
                    "genres": prog.genres if prog else [],
                })
                if len(upcoming) >= 4:
                    break

        channels_list.append({
            "id": ch.id,
            "service_id": ch.service_id,
            "call_sign": ch.call_sign,
            "name": ch.name,
            "number": ch.number,
            "sub_number": ch.sub_number,
            "channel_str": ch_num_str,
            "logo_url": ch.logo_url,
            "upcoming": upcoming,
        })

    return {
        "status": EPGStats.status,
        "source": EPGStats.source,
        "total_channels": len(channels_list),
        "total_schedules": EPGStats.schedule_count,
        "last_sync": EPGStats.last_sync.isoformat() if EPGStats.last_sync else None,
        "last_error": EPGStats.last_error,
        "channels": channels_list,
    }


@app.post("/api/epg/configure")
async def configure_epg(payload: ConfigureEPGRequest):
    """
    Save upstream provider credentials and trigger an immediate EPG sync.
    Persists configuration in /config/settings.json.
    """
    updates = {}
    if payload.schedules_direct_user is not None:
        updates["SCHEDULES_DIRECT_USER"] = payload.schedules_direct_user.strip()
    if payload.schedules_direct_pass is not None:
        updates["SCHEDULES_DIRECT_PASS"] = payload.schedules_direct_pass.strip()
    if payload.postal_code is not None:
        updates["POSTAL_CODE"] = payload.postal_code.strip()
    if payload.epg_api_key is not None:
        updates["EPG_API_KEY"] = payload.epg_api_key.strip()
    if payload.tmdb_api_key is not None:
        updates["TMDB_API_KEY"] = payload.tmdb_api_key.strip()

    settings.save_overrides(updates)

    # Trigger EPG sync pass
    try:
        pipeline = EPGPipeline()
        output_file = await pipeline.run(api_key=settings.epg_service_key)
        return {
            "status": "success",
            "message": f"Settings saved and EPG compiled ({EPGStats.channels_count} channels, {EPGStats.schedule_count} schedule entries). Source: {EPGStats.source}.",
            "stats": EPGStats.to_dict(),
        }
    except Exception as e:
        logger.error(f"Failed to synchronize EPG after configuration update: {e}")
        return {
            "status": "warning",
            "message": f"Settings saved, but EPG compilation encountered an error: {e}",
            "stats": EPGStats.to_dict(),
        }


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


@app.get("/api/metadata/movies/{movie_id_or_title}/dvdid")
async def get_movie_dvdid_xml_direct(
    movie_id_or_title: str,
    year: Optional[int] = Query(None, description="Optional release year"),
):
    """Directly preview generated WMC dvdid.xml MDR-DVD 5.0 XML."""
    xml_str = await movie_engine.get_dvdid_xml(movie_id_or_title, year)
    if not xml_str:
        raise HTTPException(status_code=404, detail="Movie metadata not found.")
    return Response(
        content=xml_str,
        media_type="application/xml",
    )


# =========================================================================
# Native Windows Media Center VideoDetails Service (Movie Details Screen)
# =========================================================================

@app.get("/videodetails/lookup/movie/{title}")
async def videodetails_lookup_movie(title: str, year: Optional[int] = Query(None)):
    """
    WMC Movie Details lookup endpoint.
    Called when user clicks on a movie in the Movie Guide.
    """
    xml_str = await movie_engine.get_videodetails_lookup_xml(title=title, year=year)
    return Response(content=xml_str, media_type="application/xml")


@app.get("/videodetails/{guid}")
async def videodetails_get_full(guid: str):
    """
    WMC Full Video Details endpoint.
    Returns extended synopsis, cast/crew contributors, and MPAA ratings.
    """
    xml_str = await movie_engine.get_videodetails_full_xml(guid)
    return Response(content=xml_str, media_type="application/xml")


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
    """Serve the modern management web suite dashboard or auto-dispatch setup for CLI/HTA clients."""
    ua = request.headers.get("user-agent", "").lower()
    accept = request.headers.get("accept", "").lower()

    # If client is PowerShell (e.g. iwr / DownloadString), immediately serve setup.ps1 directly from root
    if "powershell" in ua or ("text/plain" in accept and "text/html" not in accept):
        return await get_setup_powershell_script(request)

    # If client is MSHTA, immediately serve setup.hta directly from root
    if "mshost" in ua or "mshta" in ua:
        return await get_setup_hta(request)

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "version": __version__,
            "tz": settings.TZ,
            "host_ip": request.client.host if request.client else "localhost",
            "port": settings.PORT,
            "epg": EPGStats.to_dict(),
            "dns": DNSStats.to_dict(),
            "dns_server_ip": settings.detected_host_ip,
            "sd_user": settings.SCHEDULES_DIRECT_USER or "",
            "postal_code": settings.POSTAL_CODE or "",
            "has_sd_pass": bool(settings.SCHEDULES_DIRECT_PASS),
            "has_api_key": bool(settings.epg_service_key),
            "has_tmdb_key": bool(settings.TMDB_API_KEY),
        },
    )
