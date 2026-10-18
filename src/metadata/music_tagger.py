import io
import logging
from pathlib import Path
from typing import Any, Dict, Optional
from mutagen.id3 import (
    APIC,
    ID3,
    ID3NoHeaderError,
    TALB,
    TCON,
    TPE1,
    TPE2,
    TRCK,
    TIT2,
    TYER,
)
from mutagen.flac import FLAC, Picture
from PIL import Image, ImageDraw

logger = logging.getLogger(__name__)


def resize_to_wmc_album_art(image_bytes: bytes, target_size: tuple[int, int] = (500, 500)) -> bytes:
    """Resize arbitrary image to 500x500 JPEG optimized for WMC ehshell.exe."""
    with Image.open(io.BytesIO(image_bytes)) as img:
        # Convert to RGB if RGBA or palette
        if img.mode != "RGB":
            img = img.convert("RGB")
        resized = img.resize(target_size, Image.Resampling.LANCZOS)
        out = io.BytesIO()
        resized.save(out, format="JPEG", quality=90, optimize=True)
        return out.getvalue()


def create_fallback_album_art(artist: str, album: str) -> bytes:
    """Generate a clean 500x500 album art cover when no online art is found."""
    width, height = 500, 500
    img = Image.new("RGB", (width, height), color=(30, 41, 59))
    draw = ImageDraw.Draw(img)

    # Decorative vinyl record circle in background
    center = (width // 2, height // 2)
    draw.ellipse([50, 50, 450, 450], fill=(15, 23, 42), outline=(51, 65, 85), width=4)
    draw.ellipse([150, 150, 350, 350], fill=(30, 41, 59), outline=(71, 85, 105), width=2)
    draw.ellipse([210, 210, 290, 290], fill=(100, 116, 139))

    # Text overlay
    draw.text((width // 2, 80), artist.upper()[:28], fill=(226, 232, 240), anchor="mm")
    draw.text((width // 2, 420), album[:30], fill=(148, 163, 184), anchor="mm")

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=90)
    return out.getvalue()


class MusicTagger:
    """Tags audio files with standard ID3v2.3 tags and embeds 500x500 artwork."""

    def tag_mp3(
        self,
        file_path: Path,
        metadata: Dict[str, Any],
        artwork_bytes: Optional[bytes] = None,
    ) -> bool:
        """Apply ID3v2.3 tags and embed 500x500 album art for Windows Media Center."""
        try:
            try:
                tags = ID3(file_path)
            except ID3NoHeaderError:
                tags = ID3()

            # UTF-16 (encoding=1) is strictly required by Windows ID3v2.3 parser
            enc = 1

            if "title" in metadata:
                tags.add(TIT2(encoding=enc, text=[metadata["title"]]))
            if "artist" in metadata:
                tags.add(TPE1(encoding=enc, text=[metadata["artist"]]))
            if "album" in metadata:
                tags.add(TALB(encoding=enc, text=[metadata["album"]]))
            if "album_artist" in metadata:
                tags.add(TPE2(encoding=enc, text=[metadata["album_artist"]]))
            elif "artist" in metadata:
                tags.add(TPE2(encoding=enc, text=[metadata["artist"]]))
            if "track_number" in metadata:
                tags.add(TRCK(encoding=enc, text=[str(metadata["track_number"])]))
            if "year" in metadata and metadata["year"]:
                tags.add(TYER(encoding=enc, text=[str(metadata["year"])]))
            if "genre" in metadata:
                tags.add(TCON(encoding=enc, text=[metadata["genre"]]))

            # Embed 500x500 album art
            if artwork_bytes:
                wmc_art = resize_to_wmc_album_art(artwork_bytes)
                tags.delall("APIC")
                tags.add(
                    APIC(
                        encoding=3,
                        mime="image/jpeg",
                        type=3,  # Front cover
                        desc="Cover",
                        data=wmc_art,
                    )
                )

            # Crucial: save with v2_version=3 for WMC/WMP compatibility
            tags.save(file_path, v2_version=3)
            logger.info(f"Successfully tagged MP3 (ID3v2.3): {file_path.name}")
            return True

        except Exception as e:
            logger.error(f"Error tagging MP3 {file_path}: {e}")
            return False

    def tag_flac(
        self,
        file_path: Path,
        metadata: Dict[str, Any],
        artwork_bytes: Optional[bytes] = None,
    ) -> bool:
        """Tag FLAC files with Vorbis comments and embedded Picture block."""
        try:
            audio = FLAC(file_path)
            if "title" in metadata:
                audio["title"] = metadata["title"]
            if "artist" in metadata:
                audio["artist"] = metadata["artist"]
            if "album" in metadata:
                audio["album"] = metadata["album"]
            if "track_number" in metadata:
                audio["tracknumber"] = str(metadata["track_number"])
            if "year" in metadata and metadata["year"]:
                audio["date"] = str(metadata["year"])
            if "genre" in metadata:
                audio["genre"] = metadata["genre"]

            if artwork_bytes:
                wmc_art = resize_to_wmc_album_art(artwork_bytes)
                pic = Picture()
                pic.type = 3
                pic.mime = "image/jpeg"
                pic.desc = "Front Cover"
                pic.data = wmc_art
                audio.clear_pictures()
                audio.add_picture(pic)

            audio.save()
            logger.info(f"Successfully tagged FLAC: {file_path.name}")
            return True
        except Exception as e:
            logger.error(f"Error tagging FLAC {file_path}: {e}")
            return False

    def write_folder_artwork(self, album_dir: Path, artwork_bytes: bytes) -> tuple[Path, Path]:
        """
        Write Folder.jpg and AlbumArtSmall.jpg to album directory for WMC UI display.
        """
        album_dir.mkdir(parents=True, exist_ok=True)
        wmc_500 = resize_to_wmc_album_art(artwork_bytes, (500, 500))
        wmc_small = resize_to_wmc_album_art(artwork_bytes, (75, 75))

        folder_jpg = album_dir / "Folder.jpg"
        small_jpg = album_dir / "AlbumArtSmall.jpg"

        folder_jpg.write_bytes(wmc_500)
        small_jpg.write_bytes(wmc_small)

        # Also write lowercase folder.jpg for Linux case sensitivity
        (album_dir / "folder.jpg").write_bytes(wmc_500)

        return folder_jpg, small_jpg

