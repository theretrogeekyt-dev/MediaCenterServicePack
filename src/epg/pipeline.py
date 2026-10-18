import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Optional
import httpx

from src.config import settings
from src.epg.models import Channel, EPGDatabase, Lineup, Program, ScheduleEntry, SeriesInfo
from src.epg.mxf_generator import MXFGenerator
from src.epg.schedules_direct import SchedulesDirectClient
from src.epg.xmltv_parser import XMLTVParser

logger = logging.getLogger(__name__)


class EPGStats:
    """Tracks status and metrics of EPG compilation runs."""
    last_sync: Optional[datetime] = None
    channels_count: int = 0
    programs_count: int = 0
    schedule_count: int = 0
    status: str = "Uninitialized"
    last_error: Optional[str] = None

    @classmethod
    def to_dict(cls) -> Dict[str, Any]:
        return {
            "last_sync": cls.last_sync.isoformat() if cls.last_sync else None,
            "channels_count": cls.channels_count,
            "programs_count": cls.programs_count,
            "schedule_count": cls.schedule_count,
            "status": cls.status,
            "last_error": cls.last_error,
        }


def generate_fallback_epg() -> EPGDatabase:
    """
    Generate an out-of-the-box sample broadcast lineup (NBC, CBS, ABC, FOX, PBS)
    with 48 hours of scheduling so guide.mxf is immediately usable on fresh container startup.
    """
    db = EPGDatabase()
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

    channels_data = [
        ("ch_4_1", "s_wnbc", "WNBC-HD", "NBC (WNBC 4.1)", 4, 1, "https://upload.wikimedia.org/wikipedia/commons/e/e3/NBC_logo_%282022%29.svg"),
        ("ch_2_1", "s_wcbs", "WCBS-HD", "CBS (WCBS 2.1)", 2, 1, "https://upload.wikimedia.org/wikipedia/commons/7/77/CBS_News_and_Stations_logo.svg"),
        ("ch_7_1", "s_wabc", "WABC-HD", "ABC (WABC 7.1)", 7, 1, "https://upload.wikimedia.org/wikipedia/commons/2/22/American_Broadcasting_Company_Logo.svg"),
        ("ch_5_1", "s_wnyw", "WNYW-HD", "FOX (WNYW 5.1)", 5, 1, "https://upload.wikimedia.org/wikipedia/commons/f/ff/Fox_broadcasting_company_logo_%282019%29.svg"),
        ("ch_13_1", "s_wnet", "WNET-HD", "PBS (Thirteen 13.1)", 13, 1, "https://upload.wikimedia.org/wikipedia/commons/2/26/PBS_logo_%282019%29.svg"),
    ]

    for c_id, s_id, call, name, num, sub, logo in channels_data:
        db.channels[c_id] = Channel(
            id=c_id,
            service_id=s_id,
            call_sign=call,
            name=name,
            number=num,
            sub_number=sub,
            logo_url=logo,
        )

    db.lineup = Lineup(channels=list(db.channels.values()))

    # Sample Series & Programs
    series_samples = [
        ("si_news", "Nightly News", "Nightly national and international news coverage."),
        ("si_sitcom", "Prime Time Comedy", "Award-winning sitcom following a quirky family."),
        ("si_drama", "Metro Police Procedural", "Detectives investigate major crimes across the city."),
        ("si_nature", "Earth & Beyond", "High-definition documentary series exploring wildlife."),
    ]
    for s_id, title, desc in series_samples:
        db.series[s_id] = SeriesInfo(id=s_id, title=title, description=desc)

    programs_catalog = [
        Program(
            id="pr_1",
            title="Nightly News",
            episode_title="Evening Broadcast",
            description="Comprehensive national news, analysis, and weather.",
            is_series=True,
            is_news=True,
            series_id="si_news",
            genres=["News"],
        ),
        Program(
            id="pr_2",
            title="Prime Time Comedy",
            episode_title="The Grand Opening",
            description="The family attempts to open their new neighborhood cafe.",
            is_series=True,
            season_number=3,
            episode_number=7,
            series_id="si_sitcom",
            genres=["Comedy"],
            half_stars_rating=6,
            mpaa_rating="TV-PG",
        ),
        Program(
            id="pr_3",
            title="Metro Police Procedural",
            episode_title="Double Cross",
            description="The detectives uncover a ring of high-stakes art thieves.",
            is_series=True,
            season_number=5,
            episode_number=12,
            series_id="si_drama",
            genres=["Drama", "Action"],
            half_stars_rating=7,
            mpaa_rating="TV-14",
        ),
        Program(
            id="pr_4",
            title="Blockbuster Feature Film",
            description="A courageous astronaut embarks on an interstellar expedition to save humanity.",
            is_movie=True,
            year=2021,
            genres=["Movie", "Science Fiction", "Action"],
            half_stars_rating=8,
            mpaa_rating="PG-13",
        ),
        Program(
            id="pr_5",
            title="Earth & Beyond",
            episode_title="The Deep Oceans",
            description="Exploring the mysterious creatures inhabiting the deep trenches.",
            is_series=True,
            series_id="si_nature",
            genres=["Documentary", "Educational"],
            half_stars_rating=8,
            mpaa_rating="TV-G",
        ),
    ]

    for p in programs_catalog:
        db.programs[p.id] = p

    # Build 48h schedule for each channel
    for ch in db.channels.values():
        cur_time = now
        end_time = now + timedelta(hours=48)
        p_cycle = list(db.programs.values())
        idx = 0
        while cur_time < end_time:
            prog = p_cycle[idx % len(p_cycle)]
            dur = 7200 if prog.is_movie else 3600
            db.schedule_entries.append(
                ScheduleEntry(
                    program_id=prog.id,
                    service_id=ch.service_id,
                    start_time=cur_time,
                    duration_seconds=dur,
                    is_hdtv=True,
                    is_repeat=(idx % 2 == 1),
                    is_stereo=True,
                    is_cc=True,
                )
            )
            cur_time += timedelta(seconds=dur)
            idx += 1

    return db


class EPGPipeline:
    """Coordinates fetching, parsing, generating, and storing guide.mxf."""

    async def run(self, source_url: Optional[str] = None) -> Path:
        """Run the full EPG compilation pipeline."""
        EPGStats.status = "Running"
        logger.info("Starting EPG sync pipeline...")

        target_url = source_url or settings.XMLTV_URL
        db: Optional[EPGDatabase] = None

        try:
            # 1. Fetch from XMLTV URL if specified
            if target_url:
                logger.info(f"Fetching XMLTV feed from URL: {target_url}")
                async with httpx.AsyncClient(timeout=60.0) as client:
                    resp = await client.get(target_url)
                    resp.raise_for_status()
                    parser = XMLTVParser()
                    db = parser.parse(resp.content)

            # 2. Check local XMLTV file if URL wasn't configured
            elif settings.XMLTV_FILE and Path(settings.XMLTV_FILE).is_file():
                logger.info(f"Loading local XMLTV file: {settings.XMLTV_FILE}")
                content = Path(settings.XMLTV_FILE).read_bytes()
                parser = XMLTVParser()
                db = parser.parse(content)

            # 3. Check Schedules Direct API
            elif settings.SCHEDULES_DIRECT_USER and settings.SCHEDULES_DIRECT_PASS:
                logger.info("Querying Schedules Direct API...")
                sd = SchedulesDirectClient(
                    username=settings.SCHEDULES_DIRECT_USER,
                    password=settings.SCHEDULES_DIRECT_PASS,
                    token=settings.SCHEDULES_DIRECT_KEY,
                )
                try:
                    db = await sd.fetch_guide()
                finally:
                    await sd.close()

            # 4. Fallback default guide
            if not db or len(db.channels) == 0:
                logger.info("No external upstream feed configured or available. Generating standard broadcast guide.")
                db = generate_fallback_epg()

            # Generate MXF XML
            logger.info("Compiling EPG data into Windows Media Center MXF format...")
            generator = MXFGenerator(db)
            mxf_bytes = generator.generate()

            # Save to config directory
            output_file = settings.guide_mxf_file
            output_file.parent.mkdir(parents=True, exist_ok=True)
            output_file.write_bytes(mxf_bytes)

            # Record stats
            EPGStats.last_sync = datetime.now(timezone.utc)
            EPGStats.channels_count = len(db.channels)
            EPGStats.programs_count = len(db.programs)
            EPGStats.schedule_count = len(db.schedule_entries)
            EPGStats.status = "Healthy"
            EPGStats.last_error = None

            logger.info(
                f"Successfully generated {output_file} ({len(mxf_bytes)} bytes) with "
                f"{EPGStats.channels_count} channels and {EPGStats.schedule_count} schedule entries."
            )
            return output_file

        except Exception as e:
            logger.error(f"EPG Pipeline failed: {e}", exc_info=True)
            EPGStats.status = "Error"
            EPGStats.last_error = str(e)
            raise

