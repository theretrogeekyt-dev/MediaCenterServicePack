import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from mutagen import File as MutagenFile

from src.config import settings
from src.metadata.musicbrainz_client import MusicBrainzClient
from src.metadata.music_tagger import MusicTagger, create_fallback_album_art

logger = logging.getLogger(__name__)

AUDIO_EXTENSIONS = {".mp3", ".flac", ".wma", ".m4a"}


class MusicScannerStats:
    """Statistics for Music Library scans."""
    last_scan: Optional[datetime] = None
    scanned_count: int = 0
    tagged_count: int = 0
    errors_count: int = 0
    status: str = "Idle"
    last_error: Optional[str] = None

    @classmethod
    def to_dict(cls) -> Dict[str, Any]:
        return {
            "last_scan": cls.last_scan.isoformat() if cls.last_scan else None,
            "scanned_count": cls.scanned_count,
            "tagged_count": cls.tagged_count,
            "errors_count": cls.errors_count,
            "status": cls.status,
            "last_error": cls.last_error,
        }


def parse_track_from_filename(filename: str) -> tuple[Optional[int], str]:
    """Parse track number and title from filename like '01 - Come Together.mp3'."""
    stem = Path(filename).stem
    match = re.match(r"^(\d{1,2})[\s\.\-_]+(.+)$", stem)
    if match:
        try:
            return int(match.group(1)), match.group(2).strip()
        except ValueError:
            pass
    return None, stem.strip()


class MusicScanner:
    """Scans music directory, retrieves missing album artwork, and tags files for WMC."""

    def __init__(self):
        self.mb_client = MusicBrainzClient()
        self.tagger = MusicTagger()

    async def scan_directory(self, root_path: Optional[Path] = None, force_refresh: bool = False) -> Dict[str, int]:
        target_root = root_path or settings.music_path
        MusicScannerStats.status = "Scanning"
        logger.info(f"Scanning music library: {target_root}")

        scanned = 0
        tagged = 0
        errors = 0

        try:
            if not target_root.exists():
                logger.warning(f"Music directory does not exist: {target_root}")
                MusicScannerStats.status = "Idle"
                return {"scanned": 0, "tagged": 0, "errors": 0}

            # Collect all audio files grouped by parent directory (album directory)
            album_dirs: Dict[Path, List[Path]] = {}
            for item in target_root.rglob("*"):
                if item.is_file() and item.suffix.lower() in AUDIO_EXTENSIONS:
                    album_dirs.setdefault(item.parent, []).append(item)

            for album_dir, audio_files in album_dirs.items():
                folder_jpg = album_dir / "Folder.jpg"
                needs_folder_art = not folder_jpg.exists() or force_refresh

                # Determine Artist and Album name from directory hierarchy
                # Standard convention: /music/Artist/Album/track.mp3
                parts = album_dir.relative_to(target_root).parts
                if len(parts) >= 2:
                    dir_artist = parts[-2]
                    dir_album = parts[-1]
                elif len(parts) == 1:
                    dir_artist = "Various Artists"
                    dir_album = parts[0]
                else:
                    dir_artist = "Unknown Artist"
                    dir_album = "Unknown Album"

                # Check if we already have artwork in memory or from online
                art_bytes: Optional[bytes] = None
                if folder_jpg.exists():
                    try:
                        art_bytes = folder_jpg.read_bytes()
                    except Exception:
                        pass

                # If missing artwork, query MusicBrainz
                if not art_bytes and dir_artist != "Unknown Artist":
                    logger.info(f"Searching MusicBrainz for: {dir_artist} - {dir_album}")
                    release_info = await self.mb_client.search_release(dir_artist, dir_album)
                    if release_info and release_info.get("mbid"):
                        art_bytes = await self.mb_client.fetch_cover_art(release_info["mbid"])

                # If still no artwork, create clean fallback vinyl artwork
                if not art_bytes:
                    art_bytes = create_fallback_album_art(dir_artist, dir_album)

                # Ensure Folder.jpg exists
                if needs_folder_art and art_bytes:
                    self.tagger.write_folder_artwork(album_dir, art_bytes)

                # Tag individual audio files
                for audio_file in audio_files:
                    scanned += 1
                    try:
                        track_num, track_title = parse_track_from_filename(audio_file.name)
                        meta = {
                            "artist": dir_artist,
                            "album_artist": dir_artist,
                            "album": dir_album,
                            "title": track_title,
                            "track_number": track_num,
                        }

                        if audio_file.suffix.lower() == ".mp3":
                            success = self.tagger.tag_mp3(audio_file, meta, art_bytes)
                        elif audio_file.suffix.lower() == ".flac":
                            success = self.tagger.tag_flac(audio_file, meta, art_bytes)
                        else:
                            success = True

                        if success:
                            tagged += 1
                        else:
                            errors += 1

                    except Exception as ex:
                        logger.error(f"Error tagging file {audio_file}: {ex}")
                        errors += 1

            MusicScannerStats.last_scan = datetime.now(timezone.utc)
            MusicScannerStats.scanned_count = scanned
            MusicScannerStats.tagged_count = tagged
            MusicScannerStats.errors_count = errors
            MusicScannerStats.status = "Healthy"
            MusicScannerStats.last_error = None

            logger.info(f"Music scan finished. Scanned: {scanned}, Tagged: {tagged}, Errors: {errors}")
            return {"scanned": scanned, "tagged": tagged, "errors": errors}

        except Exception as e:
            logger.error(f"Music scanning error: {e}", exc_info=True)
            MusicScannerStats.status = "Error"
            MusicScannerStats.last_error = str(e)
            raise
        finally:
            await self.mb_client.close()

