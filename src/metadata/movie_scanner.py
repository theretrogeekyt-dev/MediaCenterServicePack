import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.config import settings
from src.metadata.dvdid_generator import DVDIDGenerator
from src.metadata.tmdb_client import TMDbClient, clean_movie_title

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
        self.tmdb_client = TMDbClient(api_key=tmdb_api_key or settings.TMDB_API_KEY)
        self.generator = DVDIDGenerator()

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
                logger.warning(f"Movies directory does not exist: {target_root}")
                MovieScannerStats.status = "Idle"
                return {"scanned": 0, "updated": 0, "errors": 0}

            # Discover movie folders or standalone movie files
            candidate_dirs: List[Path] = []

            for item in target_root.iterdir():
                if item.is_dir():
                    # Could be movie folder or VIDEO_TS parent
                    candidate_dirs.append(item)
                elif item.suffix.lower() in VIDEO_EXTENSIONS:
                    # Video file in root movies directory - movie directory is item.parent
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

                    # Query TMDb
                    movie_meta = await self.tmdb_client.search_movie(title, year)
                    if not movie_meta:
                        logger.warning(f"Metadata lookup yielded no results for: {title}")
                        continue

                    # Download poster if available
                    poster_data = None
                    if movie_meta.get("poster_url"):
                        poster_data = await self.tmdb_client.download_image(movie_meta["poster_url"])

                    # Write dvdid.xml and artwork
                    self.generator.write_sidecars(movie_dir, movie_meta, poster_data)
                    updated += 1

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
            await self.tmdb_client.close()

