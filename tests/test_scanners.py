from pathlib import Path
from lxml import etree
from mutagen.id3 import ID3
import pytest

from src.config import settings
from src.epg.pipeline import EPGPipeline, EPGStats
from src.metadata.movie_scanner import MovieScanner, MovieScannerStats
from src.metadata.music_scanner import MusicScanner, MusicScannerStats


@pytest.mark.asyncio
async def test_epg_pipeline_generation(tmp_path: Path):
    """Verify end-to-end EPG pipeline generates valid guide.mxf and updates stats."""
    pipeline = EPGPipeline()
    output_path = await pipeline.run()

    assert output_path.exists()
    assert output_path.stat().st_size > 0
    assert EPGStats.status == "Healthy"
    assert EPGStats.channels_count >= 5
    assert EPGStats.schedule_count > 0

    # Ensure XML structure is valid
    root = etree.parse(str(output_path)).getroot()
    assert root.tag == "MXF"


@pytest.mark.asyncio
async def test_movie_scanner_pipeline(tmp_path: Path):
    """Verify MovieScanner discovers movies, writes dvdid.xml and artwork sidecars."""
    movies_root = tmp_path / "movies"
    movie_folder = movies_root / "The Matrix (1999)"
    movie_folder.mkdir(parents=True, exist_ok=True)
    video_file = movie_folder / "The.Matrix.1999.mkv"
    video_file.write_bytes(b"\x00" * 1024)

    scanner = MovieScanner()
    results = await scanner.scan_directory(root_path=movies_root)

    assert results["scanned"] == 1
    assert results["updated"] == 1
    assert results["errors"] == 0

    # Verify sidecar files
    assert (movie_folder / "dvdid.xml").exists()
    assert (movie_folder / "folder.jpg").exists()
    assert (movie_folder / "poster.jpg").exists()

    # Verify dvdid.xml contents
    root = etree.parse(str(movie_folder / "dvdid.xml")).getroot()
    assert root.find(".//dvdTitle").text == "The Matrix"
    assert MovieScannerStats.status == "Healthy"


@pytest.mark.asyncio
async def test_music_scanner_pipeline(tmp_path: Path):
    """Verify MusicScanner processes album hierarchy, writes Folder.jpg and ID3v2.3 tags."""
    music_root = tmp_path / "music"
    album_folder = music_root / "Daft Punk" / "Discovery"
    album_folder.mkdir(parents=True, exist_ok=True)

    track_file = album_folder / "01 - One More Time.mp3"
    track_file.write_bytes(b"\xFF\xFB\x90\x04" + b"\x00" * 2000)

    scanner = MusicScanner()
    results = await scanner.scan_directory(root_path=music_root)

    assert results["scanned"] == 1
    assert results["tagged"] == 1
    assert results["errors"] == 0

    # Verify Folder.jpg sidecars
    assert (album_folder / "Folder.jpg").exists()
    assert (album_folder / "AlbumArtSmall.jpg").exists()

    # Verify ID3v2.3 tags
    tags = ID3(track_file)
    assert tags.version == (2, 3, 0)
    assert tags["TIT2"].text[0] == "One More Time"
    assert tags["TPE1"].text[0] == "Daft Punk"
    assert tags["TALB"].text[0] == "Discovery"
    assert len(tags.getall("APIC")) == 1
    assert MusicScannerStats.status == "Healthy"

