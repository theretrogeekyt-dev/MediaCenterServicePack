import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.config import settings
from src.metadata.movie_engine import MovieEngine
from src.metadata.tmdb_client import clean_movie_title

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".iso", ".ts", ".m4v"}


class MovieScannerStats:
    """Statistics and tracking for Movie Library scans."""
    last_scan: Optional[datetime] = None
    scanned_count: int = 0
    updated_count: int = 0
    errors_count: int = 0
    status: str = "Idle"
    last_error: Optional[str] = None

    @classmethod
    def to_dict(cls) -> Dict[str, Any]:
        return {
            "last_scan": cls.last_scan.isoformat() if cls.last_scan else None,
            "scanned_count": cls.scanned_count,
            "updated_count": cls.updated_count,
            "errors_count": cls.errors_count,
            "status": cls.status,
            "last_error": cls.last_error,
        }


class MovieScanner:
    """Scans movie shares, identifies titles, fetches TMDb data, and writes sidecars."""

    def __init__(self, tmdb_api_key: Optional[str] = None):
        self.engine = MovieEngine(tmdb_api_key=tmdb_api_key or settings.TMDB_API_KEY)

    async def scan_directory(self, root_path: Optional[Path] = None, force_refresh: bool = False) -> Dict[str, int]:
        """Scan movie directory for folders/files needing dvdid.xml and cover art."""
        target_root = root_path or settings.movies_path
        MovieScannerStats.status = "Scanning"
        logger.info(f"Scanning movies directory: {target_root}")

        scanned = 0
        updated = 0
        errors = 0

        try:
            if not target_root.exists():
                logger.info(f"Movies directory does not exist or not mounted: {target_root}")
                MovieScannerStats.status = "Idle"
                return {"scanned": 0, "updated": 0, "errors": 0}

            candidate_dirs: List[Path] = []
            for item in target_root.iterdir():
                if item.is_dir():
                    candidate_dirs.append(item)
                elif item.suffix.lower() in VIDEO_EXTENSIONS:
                    if item.parent not in candidate_dirs:
                        candidate_dirs.append(item.parent)

            for movie_dir in candidate_dirs:
                scanned += 1
                dvdid_file = movie_dir / "dvdid.xml"
                folder_jpg = movie_dir / "folder.jpg"

                if not force_refresh and dvdid_file.exists() and folder_jpg.exists():
                    logger.debug(f"Skipping already tagged movie: {movie_dir.name}")
                    continue

                try:
                    title, year = clean_movie_title(movie_dir.name)
                    logger.info(f"Processing movie '{title}' (Year: {year}) in {movie_dir}")
                    res = await self.engine.download_to_directory(title, movie_dir, year)
                    if res:
                        updated += 1
                    else:
                        errors += 1
                except Exception as ex:
                    logger.error(f"Failed processing movie folder '{movie_dir}': {ex}")
                    errors += 1

            MovieScannerStats.last_scan = datetime.now(timezone.utc)
            MovieScannerStats.scanned_count = scanned
            MovieScannerStats.updated_count = updated
            MovieScannerStats.errors_count = errors
            MovieScannerStats.status = "Healthy"
            MovieScannerStats.last_error = None

            logger.info(f"Movie scan finished. Scanned: {scanned}, Updated: {updated}, Errors: {errors}")
            return {"scanned": scanned, "updated": updated, "errors": errors}

        except Exception as e:
            logger.error(f"Movie scanning pipeline error: {e}", exc_info=True)
            MovieScannerStats.status = "Error"
            MovieScannerStats.last_error = str(e)
            raise
        finally:
            await self.engine.close()
