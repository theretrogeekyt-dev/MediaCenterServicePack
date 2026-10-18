from datetime import datetime, timezone
from pathlib import Path
from lxml import etree
import pytest

from src.epg.models import Channel, EPGDatabase, Lineup, Program, ScheduleEntry, SeriesInfo
from src.epg.mxf_generator import MXFGenerator
from src.epg.pipeline import EPGPipeline, EPGStats, generate_fallback_epg
from src.epg.schedules_direct import SchedulesDirectClient
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


@pytest.mark.asyncio
async def test_schedules_direct_client_direct_api_key(monkeypatch):
    """Verify SchedulesDirectClient utilizes API key directly without username/password."""
    client = SchedulesDirectClient(token="mock_sd_api_key_xyz")
    assert await client.login() == "mock_sd_api_key_xyz"

    async def mock_get(url, headers=None):
        class MockResp:
            def __init__(self, status_code, json_data):
                self.status_code = status_code
                self._json = json_data

            def json(self):
                return self._json

        if "/status" in url:
            return MockResp(200, {
                "account": {"expires": "2027-01-01T00:00:00Z"},
                "lineups": [{"lineup": "USA-OTA-10001", "name": "OTA Broadcast"}]
            })
        elif "/lineups/USA-OTA-10001" in url:
            return MockResp(200, {
                "stations": [
                    {"stationID": "1001", "name": "WABC HD", "callsign": "WABC", "logo": {"URL": "http://img/wabc.png"}},
                    {"stationID": "1002", "name": "WNBC HD", "callsign": "WNBC", "logo": {"URL": "http://img/wnbc.png"}},
                ],
                "map": [
                    {"stationID": "1001", "channel": "7.1"},
                    {"stationID": "1002", "channel": "4.1"},
                ]
            })
        return MockResp(404, {})

    async def mock_post(url, json=None, headers=None):
        class MockResp:
            def __init__(self, status_code, json_data):
                self.status_code = status_code
                self._json = json_data

            def json(self):
                return self._json

        if "/schedules" in url:
            return MockResp(200, [
                {
                    "stationID": "1001",
                    "programs": [
                        {
                            "programID": "EP001001001",
                            "airDateTime": "2026-10-18T20:00:00Z",
                            "duration": 3600,
                            "videoProperties": ["HDTV"],
                            "audioProperties": ["STEREO"],
                            "new": True
                        },
                        {
                            "programID": "SH002204290000",
                            "airDateTime": "2026-10-18T21:00:00Z",
                            "duration": 3600,
                        },
                        {
                            "programID": "MV999999990000",
                            "airDateTime": "2026-10-18T22:00:00Z",
                            "duration": 7200,
                        }
                    ]
                }
            ])
        elif "/programs" in url:
            return MockResp(200, [
                {
                    "programID": "EP001001001",
                    "titles": [{"title120": "Direct API Primetime"}],
                    "episodeTitle150": "The Test Episode",
                    "descriptions": {"description100": [{"description": "Great show"}]},
                    "genres": ["Drama"],
                    "entityType": "Episode"
                }
            ])
        return MockResp(404, {})

    monkeypatch.setattr(client._client, "get", mock_get)
    monkeypatch.setattr(client._client, "post", mock_post)

    db = await client.fetch_guide()
    assert db is not None
    assert len(db.channels) == 2
    assert "1001" in db.channels
    assert db.channels["1001"].call_sign == "WABC"
    assert db.channels["1001"].number == 7
    assert db.channels["1001"].sub_number == 1
    assert len(db.schedule_entries) == 3
    assert "EP001001001" in db.programs
    assert db.programs["EP001001001"].title == "Direct API Primetime"

    # Verify fallbacks for programs not returned by /programs do not use "Program SH..."
    assert "SH002204290000" in db.programs
    assert not db.programs["SH002204290000"].title.startswith("Program ")
    assert db.programs["SH002204290000"].title in ("Television Program", "Scheduled Broadcast")
    assert db.programs["MV999999990000"].title == "Feature Film"

    # Verify MXF generation produces valid XML without "Program SH..."
    generator = MXFGenerator(db)
    mxf_bytes = generator.generate()
    assert b"Assembly" in mxf_bytes
    assert b"Program SH" not in mxf_bytes

    await client.close()


@pytest.mark.asyncio
async def test_epg_pipeline_with_direct_service_api_key(monkeypatch):
    """Verify EPGPipeline uses direct service API key when available and updates stats source."""
    async def mock_fetch_guide(self):
        db = EPGDatabase(provider_id="p_direct", provider_name="ServiceDirect")
        db.channels["ch_1"] = Channel(id="ch_1", service_id="s_1", call_sign="DIRECT", name="Direct Channel", number=1)
        db.lineup = Lineup(channels=list(db.channels.values()))
        db.programs["pr_1"] = Program(id="pr_1", title="Direct Show")
        db.schedule_entries.append(
            ScheduleEntry(
                program_id="pr_1",
                service_id="s_1",
                start_time=datetime.now(timezone.utc),
                duration_seconds=3600
            )
        )
        return db

    monkeypatch.setattr(SchedulesDirectClient, "fetch_guide", mock_fetch_guide)

    pipeline = EPGPipeline()
    out = await pipeline.run(api_key="direct_secret_api_key_123")
    assert out.exists()
    assert EPGStats.status == "Healthy"
    assert EPGStats.source == "Direct Service API (Schedules Direct)"
    assert EPGStats.channels_count == 1


@pytest.mark.asyncio
async def test_epg_pipeline_permission_error_fallback(tmp_path):
    """Verify that if primary guide_mxf_file raises PermissionError, save_mxf_safely falls back gracefully."""
    # Force a primary path that triggers PermissionError on write
    class MockLockedPath:
        parent = tmp_path

        def exists(self):
            return True

        def chmod(self, mode):
            pass

        def unlink(self, missing_ok=True):
            raise PermissionError("[Errno 13] Permission denied (locked file)")

        def write_bytes(self, data):
            raise PermissionError("[Errno 13] Permission denied: '/config/guide.mxf'")

    generator = MXFGenerator(generate_fallback_epg())
    valid_mxf = generator.generate()

    # Test save_mxf_safely directly with locked target_file
    written_path = EPGPipeline.save_mxf_safely(valid_mxf, target_file=MockLockedPath())
    assert written_path.exists()
    assert b"Assembly" in written_path.read_bytes()
    assert b"Assembly" in EPGPipeline.last_mxf_bytes



