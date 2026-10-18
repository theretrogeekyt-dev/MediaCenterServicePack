import io
from pathlib import Path
from lxml import etree
from mutagen.id3 import ID3
from PIL import Image
import pytest

from src.metadata.dvdid_generator import DVDIDGenerator
from src.metadata.music_tagger import MusicTagger, resize_to_wmc_album_art
from src.metadata.tmdb_client import clean_movie_title


def test_clean_movie_title():
    """Verify movie title and year extraction from raw folder names."""
    title, year = clean_movie_title("Inception (2010)")
    assert title == "Inception"
    assert year == 2010

    title2, year2 = clean_movie_title("The.Dark.Knight.2008.1080p.BluRay.x264")
    assert title2 == "The Dark Knight"
    assert year2 == 2008

    title3, year3 = clean_movie_title("Casablanca")
    assert title3 == "Casablanca"
    assert year3 is None


def test_dvdid_xml_generation(tmp_path: Path):
    """Verify dvdid.xml MDR-DVD 5.0 schema compliance and sidecar file output."""
    generator = DVDIDGenerator()

    movie_meta = {
        "title": "Interstellar",
        "studio": "Paramount Pictures",
        "director": "Christopher Nolan",
        "lead_performers": ["Matthew McConaughey", "Anne Hathaway"],
        "mpaa_rating": "PG-13",
        "release_date": "2014-11-07",
        "genres": ["Adventure", "Drama", "Sci-Fi"],
        "overview": "A team of explorers travel through a wormhole in space.",
        "runtime": 169,
    }

    xml_bytes = generator.build_xml(movie_meta)
    assert len(xml_bytes) > 0

    root = etree.fromstring(xml_bytes)
    assert root.tag == "METADATA"

    mdr = root.find("MDR-DVD")
    assert mdr is not None
    assert mdr.find("version").text == "5.0"
    assert mdr.find("dvdTitle").text == "Interstellar"
    assert mdr.find("director").text == "Christopher Nolan"
    assert "Matthew McConaughey" in mdr.find("leadPerformer").text
    assert mdr.find("MPAARating").text == "PG-13"
    assert mdr.find("releaseDate").text == "2014-11-07"
    assert mdr.find("runtime").text == "169"
    assert mdr.find("largeCoverParams").text == "folder.jpg"

    assert root.find("DvdId") is not None
    assert root.find("DvdId").text.startswith("openmce_")

    # Write sidecars to folder
    movie_dir = tmp_path / "Interstellar (2014)"
    dvdid_file, folder_jpg = generator.write_sidecars(movie_dir, movie_meta)

    assert dvdid_file.exists()
    assert folder_jpg.exists()
    assert (movie_dir / "poster.jpg").exists()


def test_album_art_resize_and_tagging(tmp_path: Path):
    """Verify 500x500 album art resizing and strict ID3v2.3 tag writing."""
    tagger = MusicTagger()

    # 1. Create a dummy image (800x600)
    img = Image.new("RGB", (800, 600), color=(100, 150, 200))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    raw_img_bytes = buf.getvalue()

    # Resize to WMC specification
    wmc_art = resize_to_wmc_album_art(raw_img_bytes, (500, 500))
    with Image.open(io.BytesIO(wmc_art)) as loaded_img:
        assert loaded_img.size == (500, 500)
        assert loaded_img.format == "JPEG"

    # 2. Write Folder.jpg in album dir
    album_dir = tmp_path / "Pink Floyd" / "The Dark Side of the Moon"
    folder_jpg, small_jpg = tagger.write_folder_artwork(album_dir, raw_img_bytes)
    assert folder_jpg.exists()
    assert small_jpg.exists()

    with Image.open(folder_jpg) as f_img:
        assert f_img.size == (500, 500)
    with Image.open(small_jpg) as s_img:
        assert s_img.size == (75, 75)

    # 3. Create dummy MP3 and tag with ID3v2.3
    mp3_file = album_dir / "01 - Speak to Me.mp3"
    # Write a minimal valid ID3 header so Mutagen can operate
    mp3_file.write_bytes(b"\xFF\xFB\x90\x04" + b"\x00" * 2000)

    meta = {
        "title": "Speak to Me",
        "artist": "Pink Floyd",
        "album": "The Dark Side of the Moon",
        "track_number": 1,
        "year": 1973,
        "genre": "Progressive Rock",
    }

    success = tagger.tag_mp3(mp3_file, meta, raw_img_bytes)
    assert success is True

    # Read back and verify ID3v2.3 compatibility
    tags = ID3(mp3_file)
    assert tags.version == (2, 3, 0)  # Must strictly be ID3v2.3
    assert tags["TIT2"].text[0] == "Speak to Me"
    assert tags["TPE1"].text[0] == "Pink Floyd"
    assert tags["TALB"].text[0] == "The Dark Side of the Moon"
    year_frame = tags.get("TYER") or tags.get("TDRC")
    assert year_frame is not None
    assert str(year_frame.text[0]).startswith("1973")

    # Check embedded APIC frame
    apic_frames = tags.getall("APIC")
    assert len(apic_frames) == 1
    assert apic_frames[0].type == 3  # Cover (front)
    assert apic_frames[0].mime == "image/jpeg"
    with Image.open(io.BytesIO(apic_frames[0].data)) as apic_img:
        assert apic_img.size == (500, 500)
