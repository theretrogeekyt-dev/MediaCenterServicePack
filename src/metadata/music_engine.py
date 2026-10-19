import io
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
import zipfile
import httpx

from src.metadata.musicbrainz_client import MusicBrainzClient
from src.metadata.music_tagger import create_fallback_album_art, resize_to_wmc_album_art

logger = logging.getLogger(__name__)


class MusicEngine:
    """
    Direct Music Metadata & Artwork Engine.
    Downloads metadata and 500x500 album art directly from MusicBrainz, Cover Art Archive,
    and iTunes Search API without the assistance of media folders.
    """

    def __init__(self):
        self.mb_client = MusicBrainzClient()
        self._http = httpx.AsyncClient(timeout=20.0, follow_redirects=True)

    async def search_itunes(self, query: str, artist: Optional[str] = None, limit: int = 5) -> List[Dict[str, Any]]:
        """Search iTunes for album releases."""
        term = f"{artist} {query}".strip() if artist else query
        try:
            resp = await self._http.get(
                "https://itunes.apple.com/search",
                params={"term": term, "entity": "album", "limit": str(limit)},
            )
            if resp.status_code == 200:
                results = resp.json().get("results", [])
                out = []
                for item in results:
                    coll_id = str(item.get("collectionId", ""))
                    art = (item.get("artworkUrl100") or "").replace("100x100bb", "600x600bb")
                    out.append({
                        "mbid": f"itunes-{coll_id}",
                        "itunes_id": coll_id,
                        "title": item.get("collectionName", "Unknown Album"),
                        "artist": item.get("artistName", "Unknown Artist"),
                        "year": (item.get("releaseDate") or "2020")[:4],
                        "date": (item.get("releaseDate") or "2020-01-01")[:10],
                        "track_count": item.get("trackCount", 0),
                        "artwork_url": art,
                    })
                return out
        except Exception as e:
            logger.debug(f"iTunes search failed: {e}")
        return []

    async def get_itunes_release_details(self, itunes_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve full album metadata and tracklist from iTunes."""
        try:
            clean_id = itunes_id.replace("itunes-", "")
            resp = await self._http.get(
                "https://itunes.apple.com/lookup",
                params={"id": clean_id, "entity": "song"},
            )
            if resp.status_code == 200:
                items = resp.json().get("results", [])
                if items:
                    album = items[0]
                    tracks = []
                    for idx, t in enumerate(items[1:], start=1):
                        if t.get("wrapperType") == "track":
                            tracks.append({
                                "position": t.get("trackNumber", idx),
                                "title": t.get("trackName", f"Track {idx}"),
                                "length_ms": t.get("trackTimeMillis", 180000),
                            })
                    art = (album.get("artworkUrl100") or "").replace("100x100bb", "600x600bb")
                    return {
                        "mbid": f"itunes-{clean_id}",
                        "title": album.get("collectionName", "Unknown Album"),
                        "artist": album.get("artistName", "Unknown Artist"),
                        "year": (album.get("releaseDate") or "2020")[:4],
                        "date": (album.get("releaseDate") or "2020-01-01")[:10],
                        "country": album.get("country", "US"),
                        "track_count": len(tracks) or album.get("trackCount", 0),
                        "tracks": tracks,
                        "artwork_url": art,
                    }
        except Exception as e:
            logger.debug(f"iTunes lookup failed: {e}")
        return None

    async def search(self, query: str, artist: Optional[str] = None, limit: int = 10) -> List[Dict[str, Any]]:
        """Directly search MusicBrainz with seamless iTunes fallback."""
        results = await self.mb_client.search_releases(query=query, artist=artist, limit=limit)
        if not results:
            results = await self.search_itunes(query=query, artist=artist, limit=limit)
        return results

    async def get_album_metadata(self, mbid: str) -> Optional[Dict[str, Any]]:
        """Directly fetch full album release metadata and tracklist."""
        if str(mbid).startswith("itunes-"):
            return await self.get_itunes_release_details(mbid)
        data = await self.mb_client.get_release_details(mbid)
        if not data:
            data = await self.get_itunes_release_details(mbid)
        return data

    async def get_wmc_package(self, mbid_or_album: str, artist: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Directly compile a complete WMC-ready album art and metadata bundle.
        Fetches release data, downloads front cover, resizes to 500x500 and 75x75,
        and packages into an in-memory ZIP ready to download or inject into WMC art_cache.
        """
        release_info = None
        target_str = str(mbid_or_album)

        if target_str.startswith("itunes-"):
            release_info = await self.get_itunes_release_details(target_str)
        elif len(target_str) == 36 and "-" in target_str:
            release_info = await self.mb_client.get_release_details(target_str)
        else:
            candidates = await self.mb_client.search_releases(query=target_str, artist=artist, limit=1)
            if candidates:
                release_info = await self.mb_client.get_release_details(candidates[0]["mbid"])

            if not release_info:
                itunes_cands = await self.search_itunes(query=target_str, artist=artist, limit=1)
                if itunes_cands:
                    release_info = await self.get_itunes_release_details(itunes_cands[0]["mbid"])

        if not release_info:
            is_mbid = len(target_str) == 36 and "-" in target_str
            release_info = {
                "mbid": target_str if is_mbid else "00000000-0000-0000-0000-000000000000",
                "title": "Album" if is_mbid else target_str,
                "artist": artist or "Unknown Artist",
                "year": "2020",
                "date": "2020-01-01",
                "country": "US",
                "track_count": 10,
                "tracks": [],
            }

        mbid = release_info["mbid"]
        album_title = release_info.get("title", "Unknown Album")
        artist_name = release_info.get("artist", "Unknown Artist")

        # 1. Fetch artwork from Cover Art Archive or iTunes
        raw_art = None
        if release_info.get("artwork_url"):
            try:
                art_resp = await self._http.get(release_info["artwork_url"])
                if art_resp.status_code == 200 and len(art_resp.content) > 500:
                    raw_art = art_resp.content
            except Exception:
                pass

        if not raw_art and not str(mbid).startswith("itunes-"):
            raw_art = await self.mb_client.fetch_cover_art(mbid)

        if not raw_art:
            # Try iTunes artwork search as fallback
            itunes_search = await self.search_itunes(album_title, artist_name, limit=1)
            if itunes_search and itunes_search[0].get("artwork_url"):
                try:
                    art_resp = await self._http.get(itunes_search[0]["artwork_url"])
                    if art_resp.status_code == 200 and len(art_resp.content) > 500:
                        raw_art = art_resp.content
                except Exception:
                    pass

        if not raw_art:
            raw_art = create_fallback_album_art(artist_name, album_title)

        # 2. Resize to WMC standards
        wmc_500 = resize_to_wmc_album_art(raw_art, (500, 500))
        wmc_small = resize_to_wmc_album_art(raw_art, (75, 75))

        # 3. Create metadata JSON payload
        metadata_json = json.dumps(release_info, indent=2).encode("utf-8")

        # 4. Create In-Memory ZIP Package
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("Folder.jpg", wmc_500)
            zf.writestr("folder.jpg", wmc_500)
            zf.writestr("AlbumArtSmall.jpg", wmc_small)
            zf.writestr("metadata.json", metadata_json)

        zip_bytes = zip_buffer.getvalue()

        safe_filename = f"{artist_name}_{album_title}".replace(" ", "_").replace("/", "_")
        return {
            "release": release_info,
            "mbid": mbid,
            "folder_jpg": wmc_500,
            "small_jpg": wmc_small,
            "metadata_json": metadata_json,
            "zip_package": zip_bytes,
            "filename": f"{safe_filename}_wmc_art.zip",
        }

    async def download_to_directory(
        self,
        mbid_or_album: str,
        target_dir: Path,
        artist: Optional[str] = None,
    ) -> Optional[Path]:
        """
        Directly download album artwork and metadata into a specified directory.
        Requires zero pre-existing audio files.
        """
        pkg = await self.get_wmc_package(mbid_or_album, artist)
        if not pkg:
            logger.warning(f"Failed to fetch music package for: {mbid_or_album}")
            return None

        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "Folder.jpg").write_bytes(pkg["folder_jpg"])
        (target_dir / "folder.jpg").write_bytes(pkg["folder_jpg"])
        (target_dir / "AlbumArtSmall.jpg").write_bytes(pkg["small_jpg"])
        (target_dir / "metadata.json").write_bytes(pkg["metadata_json"])

        logger.info(f"Directly downloaded WMC music artwork for '{pkg['release']['title']}' to {target_dir}")
        return target_dir

    async def close(self):
        await self.mb_client.close()
        await self._http.aclose()

