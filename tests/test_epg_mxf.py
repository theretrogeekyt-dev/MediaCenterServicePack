from datetime import datetime, timezone
from pathlib import Path
from lxml import etree
import pytest

from src.epg.models import Channel, EPGDatabase, Lineup, Program, ScheduleEntry, SeriesInfo
from src.epg.mxf_generator import MXFGenerator
from src.epg.pipeline import EPGPipeline, EPGStats
from src.epg.xmltv_parser import XMLTVParser, parse_xmltv_date

MOCK_XMLTV = b"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE tv SYSTEM "xmltv.dtd">
<tv source-info-url="http://example.com" source-info-name="Example Guide">
  <channel id="I4.1.example.com">
    <display-name>4.1 WNBC</display-name>
    <display-name>WNBC-HD</display-name>
    <icon src="http://example.com/wnbc.png" />
  </channel>
  <channel id="I7.1.example.com">
    <display-name>7.1 WABC</display-name>
    <display-name>WABC-HD</display-name>
  </channel>

  <!-- Programme 1: Series Episode -->
  <programme start="20261018200000 +0000" stop="20261018210000 +0000" channel="I4.1.example.com">
    <title lang="en">The Great Mystery</title>
    <sub-title lang="en">The Beginning</sub-title>
    <desc lang="en">Detectives arrive at the secluded mansion.</desc>
    <category lang="en">Drama</category>
    <category lang="en">Mystery</category>
    <episode-num system="xmltv_ns">1.4.0/1</episode-num>
    <video><quality>HDTV</quality></video>
    <rating system="VCHIP"><value>TV-14</value></rating>
    <star-rating><value>3.5/4</value></star-rating>
    <previously-shown />
    <subtitles type="teletext" />
  </programme>

  <!-- Programme 2: Feature Film -->
  <programme start="20261018210000 +0000" stop="20261018230000 +0000" channel="I4.1.example.com">
    <title lang="en">Galactic Journey</title>
    <desc lang="en">Astronauts travel to a new star system.</desc>
    <category lang="en">Movie</category>
    <category lang="en">Science Fiction</category>
    <video><quality>HDTV</quality></video>
    <rating system="MPAA"><value>PG-13</value></rating>
    <icon src="http://example.com/galactic.jpg" />
  </programme>
</tv>
"""


def test_parse_xmltv_date():
    """Verify ISO/XMLTV date string parsing with offsets."""
    dt = parse_xmltv_date("20261018200000 +0000")
    assert dt is not None
    assert dt.year == 2026
    assert dt.month == 10
    assert dt.day == 18
    assert dt.hour == 20
    assert dt.tzinfo == timezone.utc

    # Negative offset (-0400 = EDT -> should convert to +4 UTC)
    dt_edt = parse_xmltv_date("20261018200000 -0400")
    assert dt_edt is not None
    assert dt_edt.hour == 0  # Next day 00:00 UTC


def test_xmltv_parser():
    """Verify XMLTV ingestion into internal EPGDatabase data models."""
    parser = XMLTVParser()
    db = parser.parse(MOCK_XMLTV)

    # Validate Channels
    assert len(db.channels) == 2
    wnbc = db.channels["I4.1.example.com"]
    assert wnbc.number == 4
    assert wnbc.sub_number == 1
    assert wnbc.call_sign == "4.1 WNBC"
    assert wnbc.logo_url == "http://example.com/wnbc.png"

    # Validate Lineup
    assert len(db.lineup.channels) == 2

    # Validate Programs
    assert len(db.programs) == 2
    p1 = list(db.programs.values())[0]
    assert p1.title == "The Great Mystery"
    assert p1.episode_title == "The Beginning"
    assert p1.season_number == 2  # 0-indexed: 1 + 1 = 2
    assert p1.episode_number == 5  # 0-indexed: 4 + 1 = 5
    assert p1.is_series is True
    assert p1.is_movie is False
    assert p1.mpaa_rating == "TV-14"
    assert p1.half_stars_rating == 7  # 3.5 / 4.0 * 8 = 7

    p2 = list(db.programs.values())[1]
    assert p2.title == "Galactic Journey"
    assert p2.is_movie is True
    assert p2.image_url == "http://example.com/galactic.jpg"

    # Validate SeriesInfos
    assert len(db.series) == 1
    s_info = list(db.series.values())[0]
    assert s_info.title == "The Great Mystery"
    assert p1.series_id == s_info.id

    # Validate Schedule Entries
    assert len(db.schedule_entries) == 2
    s1 = db.schedule_entries[0]
    assert s1.duration_seconds == 3600
    assert s1.is_hdtv is True
    assert s1.is_repeat is True
    assert s1.is_cc is True


def test_mxf_generator_schema_compliance():
    """Verify outputted guide.mxf meets Microsoft Media Center XML schema specifications."""
    parser = XMLTVParser()
    db = parser.parse(MOCK_XMLTV)

    generator = MXFGenerator(db)
    mxf_bytes = generator.generate()

    assert len(mxf_bytes) > 0
    # Must be valid XML parseable by lxml
    root = etree.fromstring(mxf_bytes)
    assert root.tag == "MXF"

    # Check McStore.dll assembly registration
    assembly = root.find("Assembly")
    assert assembly is not None
    assert assembly.get("name") == "McStore.dll"
    namespace = assembly.find("NameSpace")
    assert namespace is not None
    assert namespace.get("name") == "Microsoft.MediaCenter.Store"

    # Check Providers block
    provider = root.find(".//Providers/Provider")
    assert provider is not None
    assert provider.get("id") == db.provider_id

    # Check With provider block
    with_elem = root.find("With")
    assert with_elem is not None
    assert with_elem.get("provider") == db.provider_id

    # Check Keywords
    keywords = with_elem.find("Keywords")
    assert keywords is not None
    assert len(keywords.findall("Keyword")) >= 10

    # Check GuideImages
    images = with_elem.find("GuideImages")
    assert images is not None
    img_urls = [gi.get("imageUrl") for gi in images.findall("GuideImage")]
    assert "http://example.com/wnbc.png" in img_urls
    assert "http://example.com/galactic.jpg" in img_urls

    # Check Programs
    programs = with_elem.find("Programs")
    assert programs is not None
    prog_elems = programs.findall("Program")
    assert len(prog_elems) == 2

    # Check SeriesInfos
    series = with_elem.find("SeriesInfos")
    assert series is not None
    assert len(series.findall("SeriesInfo")) == 1

    # Check Services
    services = with_elem.find("Services")
    assert services is not None
    assert len(services.findall("Service")) == 2

    # Check ScheduleEntries
    sched_groups = with_elem.findall("ScheduleEntries")
    assert len(sched_groups) >= 1
    entries = sched_groups[0].findall("ScheduleEntry")
    assert len(entries) == 2
    for entry in entries:
        assert entry.get("startTime") is not None
        assert int(entry.get("duration")) > 0
        assert entry.get("isHdtv") in ("true", "false")

    # Check Lineups and Channels
    lineup = with_elem.find(".//Lineups/Lineup")
    assert lineup is not None
    channels = lineup.findall(".//Channel")
    assert len(channels) == 2
    c1 = channels[0]
    assert c1.get("number") == "4"
    assert c1.get("subNumber") == "1"
    assert c1.get("matchName") == "4.1 WNBC"


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


