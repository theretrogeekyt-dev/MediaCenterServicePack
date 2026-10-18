import logging
import re
from typing import Any, Dict, List, Optional
import httpx

logger = logging.getLogger(__name__)

TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p"


def clean_movie_title(raw_name: str) -> tuple[str, Optional[int]]:
    """
    Parse title and year from movie folder/file names.
    e.g., 'The.Dark.Knight.2008.1080p.BluRay' -> ('The Dark Knight', 2008)
    e.g., 'Inception (2010)' -> ('Inception', 2010)
    """
    # Remove file extension if present
    name = re.sub(r"\.(mp4|mkv|avi|iso|m4v|ts|vob)$", "", raw_name, flags=re.IGNORECASE)

    # Extract 4-digit year if present
    year = None
    year_match = re.search(r"[\(\[\s\._](\d{4})[\)\]\s\._]", name)
    if year_match:
        try:
            year_val = int(year_match.group(1))
            if 1900 <= year_val <= 2099:
                year = year_val
                # Slice name up to year
                name = name[: year_match.start()]
        except ValueError:
            pass

    # Clean release group tags and formatting
    name = re.sub(r"[\._]", " ", name)
    name = re.sub(r"(1080p|720p|2160p|4k|bluray|web-dl|dvdrip|x264|x265|aac|dts).*", "", name, flags=re.IGNORECASE)
    cleaned = name.strip()
    return cleaned, year


class TMDbClient:
    """Client for querying movie metadata and artwork from The Movie Database (TMDb)."""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key
        self._client = httpx.AsyncClient(timeout=20.0)

    async def search_movie(self, title: str, year: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Search TMDb for a movie by title and optional release year."""
        if not self.api_key:
            logger.debug("No TMDB_API_KEY configured; returning synthetic metadata.")
            return {
                "id": abs(hash(title)) % 1000000,
                "title": title,
                "release_date": f"{year}-01-01" if year else "2020-01-01",
                "overview": f"A feature film titled {title}.",
                "genres": ["Drama"],
                "director": "Unknown Director",
                "lead_performers": ["Featured Cast"],
                "poster_url": None,
                "runtime": 120,
            }

        params: Dict[str, Any] = {
            "api_key": self.api_key,
            "query": title,
            "include_adult": "false",
        }
        if year:
            params["year"] = str(year)

        try:
            resp = await self._client.get(f"{TMDB_BASE_URL}/search/movie", params=params)
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results", [])
            if not results:
                # If searching with year yielded nothing, retry without year
                if year:
                    params.pop("year")
                    resp = await self._client.get(f"{TMDB_BASE_URL}/search/movie", params=params)
                    data = resp.json()
                    results = data.get("results", [])

            if not results:
                logger.warning(f"No TMDb results found for: {title}")
                return None

            movie_id = results[0]["id"]
            return await self.get_movie_details(movie_id)

        except Exception as e:
            logger.error(f"Error searching TMDb for '{title}': {e}")
            return None

    async def get_movie_details(self, movie_id: int) -> Optional[Dict[str, Any]]:
        """Fetch comprehensive movie details including cast, director, and poster."""
        if not self.api_key:
            return None

        params = {
            "api_key": self.api_key,
            "append_to_response": "credits,release_dates",
        }

        try:
            resp = await self._client.get(f"{TMDB_BASE_URL}/movie/{movie_id}", params=params)
            resp.raise_for_status()
            d = resp.json()

            # Extract director
            credits = d.get("credits", {})
            crew = credits.get("crew", [])
            directors = [c["name"] for c in crew if c.get("job") == "Director"]
            director = directors[0] if directors else "Unknown Director"

            # Extract top 3 cast members
            cast = credits.get("cast", [])
            lead_performers = [c["name"] for c in cast[:3]]

            # Extract MPAA rating
            mpaa_rating = "NR"
            for release in d.get("release_dates", {}).get("results", []):
                if release.get("iso_3166_1") == "US":
                    for r in release.get("release_dates", []):
                        cert = r.get("certification")
                        if cert:
                            mpaa_rating = cert
                            break

            # Extract poster URL
            poster_path = d.get("poster_path")
            poster_url = f"{TMDB_IMAGE_BASE}/w780{poster_path}" if poster_path else None

            # Studio / Production companies
            companies = [c["name"] for c in d.get("production_companies", [])]
            studio = companies[0] if companies else "Independent"

            return {
                "id": d.get("id"),
                "title": d.get("title", ""),
                "original_title": d.get("original_title", ""),
                "overview": d.get("overview", ""),
                "release_date": d.get("release_date", ""),
                "runtime": d.get("runtime", 0) or 120,
                "genres": [g["name"] for g in d.get("genres", [])],
                "studio": studio,
                "director": director,
                "lead_performers": lead_performers,
                "mpaa_rating": mpaa_rating,
                "poster_url": poster_url,
            }

        except Exception as e:
            logger.error(f"Error fetching movie {movie_id} from TMDb: {e}")
            return None

    async def download_image(self, url: str) -> Optional[bytes]:
        """Download binary image data from TMDb."""
        try:
            resp = await self._client.get(url)
            if resp.status_code == 200:
                return resp.content
        except Exception as e:
            logger.error(f"Failed to download image from {url}: {e}")
        return None

    async def close(self):
        await self._client.aclose()

