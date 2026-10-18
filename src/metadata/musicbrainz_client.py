import asyncio
import logging
from typing import Any, Dict, List, Optional
import httpx

logger = logging.getLogger(__name__)

MB_BASE_URL = "https://musicbrainz.org/ws/2"
CAA_BASE_URL = "https://coverartarchive.org/release"
USER_AGENT = "MediaCenterServicePack/1.0 ( https://github.com/theretrogeekyt-dev/MediaCenterServicePack )"


class MusicBrainzClient:
    """Client for querying MusicBrainz and Cover Art Archive APIs directly."""

    def __init__(self):
        self._client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=25.0,
        )
        self._last_request_time = 0.0

    async def _rate_limit(self):
        """Respect MusicBrainz 1 req/sec guidelines."""
        loop = asyncio.get_event_loop()
        now = loop.time()
        elapsed = now - self._last_request_time
        if elapsed < 1.0:
            await asyncio.sleep(1.0 - elapsed)
        self._last_request_time = loop.time()

    async def search_releases(
        self, query: str, artist: Optional[str] = None, limit: int = 5
    ) -> List[Dict[str, Any]]:
        """Directly search MusicBrainz for releases matching query and artist."""
        await self._rate_limit()

        if artist:
            q = f'artist:"{artist}" AND release:"{query}"'
        else:
            q = query

        params = {"query": q, "fmt": "json", "limit": str(limit)}

        try:
            resp = await self._client.get(f"{MB_BASE_URL}/release", params=params)
            if resp.status_code != 200:
                logger.warning(f"MusicBrainz search returned status {resp.status_code}")
                return []

            data = resp.json()
            releases = data.get("releases", [])

            output = []
            for r in releases[:limit]:
                artist_credit = r.get("artist-credit", [{}])[0].get("name", "Unknown Artist")
                date_str = r.get("date", "")
                year = date_str[:4] if len(date_str) >= 4 else ""
                track_count = r.get("track-count", 0)

                output.append({
                    "mbid": r.get("id"),
                    "title": r.get("title", ""),
                    "artist": artist_credit,
                    "date": date_str,
                    "year": year,
                    "country": r.get("country", ""),
                    "track_count": track_count,
                    "barcode": r.get("barcode", ""),
                })
            return output

        except Exception as e:
            logger.error(f"Error querying MusicBrainz search for '{query}': {e}")
            return []

    async def search_release(self, artist: str, album: str) -> Optional[Dict[str, Any]]:
        """Directly search top release candidate by artist and album."""
        results = await self.search_releases(query=album, artist=artist, limit=1)
        if not results:
            return None
        return results[0]

    async def get_release_details(self, mbid: str) -> Optional[Dict[str, Any]]:
        """Directly fetch full release metadata including track listings and recordings."""
        await self._rate_limit()
        params = {"inc": "recordings+artists+labels", "fmt": "json"}

        try:
            resp = await self._client.get(f"{MB_BASE_URL}/release/{mbid}", params=params)
            if resp.status_code != 200:
                logger.warning(f"Failed to fetch release details for {mbid}: {resp.status_code}")
                return None

            data = resp.json()
            artist_credit = data.get("artist-credit", [{}])[0].get("name", "Unknown Artist")
            date_str = data.get("date", "")
            year = date_str[:4] if len(date_str) >= 4 else ""

            # Extract tracklist
            tracks = []
            for medium in data.get("media", []):
                for track in medium.get("tracks", []):
                    rec = track.get("recording", {})
                    tracks.append({
                        "position": track.get("position"),
                        "number": track.get("number"),
                        "title": track.get("title") or rec.get("title", ""),
                        "length_ms": track.get("length") or rec.get("length", 0),
                    })

            return {
                "mbid": mbid,
                "title": data.get("title", ""),
                "artist": artist_credit,
                "date": date_str,
                "year": year,
                "country": data.get("country", ""),
                "barcode": data.get("barcode", ""),
                "track_count": len(tracks),
                "tracks": tracks,
            }

        except Exception as e:
            logger.error(f"Error retrieving release {mbid} from MusicBrainz: {e}")
            return None

    async def fetch_cover_art(self, mbid: str) -> Optional[bytes]:
        """Directly download front cover artwork from Cover Art Archive."""
        url = f"{CAA_BASE_URL}/{mbid}/front-500"
        try:
            resp = await self._client.get(url, follow_redirects=True)
            if resp.status_code == 200:
                return resp.content
            # Fallback to general front cover
            resp_fallback = await self._client.get(f"{CAA_BASE_URL}/{mbid}/front", follow_redirects=True)
            if resp_fallback.status_code == 200:
                return resp_fallback.content
        except Exception as e:
            logger.debug(f"Cover art not found on CAA for MBID {mbid}: {e}")
        return None

    async def close(self):
        await self._client.aclose()
