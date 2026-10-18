import asyncio
import logging
from typing import Any, Dict, List, Optional
import httpx

logger = logging.getLogger(__name__)

MB_BASE_URL = "https://musicbrainz.org/ws/2"
CAA_BASE_URL = "https://coverartarchive.org/release"
USER_AGENT = "Project-eHome/1.0 ( https://github.com/OpenMCE/Project-eHome )"


class MusicBrainzClient:
    """Client for querying MusicBrainz and Cover Art Archive APIs."""

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

    async def search_release(self, artist: str, album: str) -> Optional[Dict[str, Any]]:
        """Search MusicBrainz for a release by artist and album title."""
        await self._rate_limit()
        query = f'artist:"{artist}" AND release:"{album}"'
        params = {"query": query, "fmt": "json", "limit": "3"}

        try:
            resp = await self._client.get(f"{MB_BASE_URL}/release", params=params)
            if resp.status_code != 200:
                logger.warning(f"MusicBrainz search returned status {resp.status_code}")
                return None

            data = resp.json()
            releases = data.get("releases", [])
            if not releases:
                # Try broader search without quotes
                params["query"] = f"{artist} {album}"
                await self._rate_limit()
                resp = await self._client.get(f"{MB_BASE_URL}/release", params=params)
                if resp.status_code == 200:
                    releases = resp.json().get("releases", [])

            if not releases:
                return None

            # Pick best match
            match = releases[0]
            mbid = match.get("id")
            title = match.get("title", album)
            artist_credit = match.get("artist-credit", [{}])[0].get("name", artist)
            date_str = match.get("date", "")
            year = date_str[:4] if len(date_str) >= 4 else None

            return {
                "mbid": mbid,
                "album": title,
                "artist": artist_credit,
                "year": year,
                "date": date_str,
            }

        except Exception as e:
            logger.error(f"Error querying MusicBrainz for '{artist} - {album}': {e}")
            return None

    async def fetch_cover_art(self, mbid: str) -> Optional[bytes]:
        """Fetch front album cover artwork from Cover Art Archive."""
        url = f"{CAA_BASE_URL}/{mbid}/front-500"
        try:
            resp = await self._client.get(url, follow_redirects=True)
            if resp.status_code == 200:
                return resp.content
            # Fallback to default front
            resp_fallback = await self._client.get(f"{CAA_BASE_URL}/{mbid}/front", follow_redirects=True)
            if resp_fallback.status_code == 200:
                return resp_fallback.content
        except Exception as e:
            logger.debug(f"Cover art not found on CAA for MBID {mbid}: {e}")
        return None

    async def close(self):
        await self._client.aclose()

