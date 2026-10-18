import io
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
import zipfile

from src.config import settings
from src.metadata.dvdid_generator import DVDIDGenerator, create_fallback_poster, generate_dvd_id
from src.metadata.tmdb_client import TMDbClient, clean_movie_title

logger = logging.getLogger(__name__)


class MovieEngine:
    """
    Direct Movie Metadata Engine.
    Downloads metadata and artwork directly from TMDb without the assistance of media folders.
    """

    def __init__(self, tmdb_api_key: Optional[str] = None):
        self.tmdb_client = TMDbClient(api_key=tmdb_api_key or settings.TMDB_API_KEY)
        self.generator = DVDIDGenerator()

    async def search(self, query: str, year: Optional[int] = None, limit: int = 10) -> List[Dict[str, Any]]:
        """Search TMDb directly for movies matching query."""
        return await self.tmdb_client.search_movies(query, year, limit=limit)

    async def get_movie_metadata(self, movie_id: int) -> Optional[Dict[str, Any]]:
        """Directly retrieve full movie metadata by TMDb ID."""
        return await self.tmdb_client.get_movie_details(movie_id)

    async def get_wmc_package(self, movie_id_or_title: str, year: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """
        Directly compile a complete WMC-ready metadata and artwork bundle.
        Returns dictionary containing dvdid.xml bytes, artwork bytes, and a ready-to-stream ZIP buffer.
        """
        # Determine if input is integer ID or title search string
        movie_meta = None
        id_str = str(movie_id_or_title)
        if id_str.isdigit():
            movie_meta = await self.tmdb_client.get_movie_details(int(id_str))
        else:
            movie_meta = await self.tmdb_client.search_movie(id_str, year)

        if not movie_meta:
            return None

        title = movie_meta.get("title", "Unknown Title")
        release_date = movie_meta.get("release_date", "")
        rel_year = release_date[:4] if len(release_date) >= 4 else ""
        dvd_id = movie_meta.get("dvd_id") or generate_dvd_id(title, rel_year)
        movie_meta["dvd_id"] = dvd_id

        # 1. Generate 5.0 MDR-DVD XML
        dvdid_xml_bytes = self.generator.build_xml(movie_meta)

        # 2. Fetch or generate artwork
        poster_bytes = None
        if movie_meta.get("poster_url"):
            poster_bytes = await self.tmdb_client.download_image(movie_meta["poster_url"])

        if not poster_bytes:
            poster_bytes = create_fallback_poster(title, rel_year)

        # 3. Create In-Memory ZIP Package
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("dvdid.xml", dvdid_xml_bytes)
            zf.writestr("folder.jpg", poster_bytes)
            zf.writestr("poster.jpg", poster_bytes)
            # Also include DvdInfoCache format for direct WMC cache injection
            zf.writestr(f"{dvd_id}.xml", dvdid_xml_bytes)

        zip_bytes = zip_buffer.getvalue()

        return {
            "movie": movie_meta,
            "dvd_id": dvd_id,
            "dvdid_xml": dvdid_xml_bytes,
            "artwork": poster_bytes,
            "zip_package": zip_bytes,
            "filename": f"{title.replace(' ', '_')}_wmc_metadata.zip",
        }

    async def download_to_directory(
        self,
        movie_id_or_title: str,
        target_dir: Path,
        year: Optional[int] = None,
    ) -> Optional[Path]:
        """
        Directly download movie metadata and artwork into a specified directory.
        Requires zero pre-existing video or media files.
        """
        pkg = await self.get_wmc_package(movie_id_or_title, year)
        if not pkg:
            logger.warning(f"Failed to fetch metadata for: {movie_id_or_title}")
            return None

        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "dvdid.xml").write_bytes(pkg["dvdid_xml"])
        (target_dir / "folder.jpg").write_bytes(pkg["artwork"])
        (target_dir / "poster.jpg").write_bytes(pkg["artwork"])

        logger.info(f"Directly downloaded WMC metadata for '{pkg['movie']['title']}' to {target_dir}")
        return target_dir

    async def get_dvdid_xml(self, movie_id_or_title: str, year: Optional[int] = None) -> Optional[str]:
        """Directly generate and return dvdid.xml string for previewing."""
        pkg = await self.get_wmc_package(movie_id_or_title, year)
        if not pkg:
            return None
        return pkg["dvdid_xml"].decode("utf-8")

    async def close(self):
        await self.tmdb_client.close()

