import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple
from lxml import etree

from src.epg.models import Channel, EPGDatabase, Lineup, Program, ScheduleEntry, SeriesInfo

logger = logging.getLogger(__name__)


def parse_xmltv_date(date_str: Optional[str]) -> Optional[datetime]:
    """
    Parse XMLTV date format, e.g. '20261018200000 +0000' or '20261018200000'.
    Returns UTC-aware datetime.
    """
    if not date_str:
        return None

    clean = date_str.strip()
    match = re.match(r"^(\d{14})(?:\s*([+-]\d{4}))?", clean)
    if not match:
        match_short = re.match(r"^(\d{8})", clean)
        if match_short:
            try:
                dt = datetime.strptime(match_short.group(1), "%Y%m%d")
                return dt.replace(tzinfo=timezone.utc)
            except ValueError:
                return None
        return None

    dt_str = match.group(1)
    tz_str = match.group(2)

    try:
        dt = datetime.strptime(dt_str, "%Y%m%d%H%M%S")
    except ValueError:
        return None

    if tz_str:
        sign = -1 if tz_str[0] == "-" else 1
        hours = int(tz_str[1:3])
        minutes = int(tz_str[3:5])
        offset = timezone(sign * timedelta(hours=hours, minutes=minutes))
        dt = dt.replace(tzinfo=offset)
        return dt.astimezone(timezone.utc)
    else:
        return dt.replace(tzinfo=timezone.utc)


def parse_episode_numbers(elem: etree._Element) -> Tuple[Optional[int], Optional[int]]:
    """Extract season and episode numbers from XMLTV episode-num tags."""
    season_num = None
    episode_num = None

    for ep_elem in elem.findall("episode-num"):
        system = ep_elem.get("system", "").lower()
        text = ep_elem.text.strip() if ep_elem.text else ""

        if system == "xmltv_ns":
            # Format: 'season - 1 . episode - 1 . part - 1' (0-indexed)
            parts = text.split(".")
            if len(parts) >= 2:
                try:
                    s_part = parts[0].split("/")[0].strip()
                    if s_part:
                        season_num = int(s_part) + 1
                except ValueError:
                    pass
                try:
                    e_part = parts[1].split("/")[0].strip()
                    if e_part:
                        episode_num = int(e_part) + 1
                except ValueError:
                    pass
            if season_num or episode_num:
                break
        elif system in ("onscreen", "common"):
            # S01E02 format
            match = re.search(r"s(\d+)e(\d+)", text, re.IGNORECASE)
            if match:
                season_num = int(match.group(1))
                episode_num = int(match.group(2))
                break

    return season_num, episode_num


class XMLTVParser:
    """Parses XMLTV files/strings into standard EPGDatabase models."""

    def parse(self, xml_content: bytes) -> EPGDatabase:
        db = EPGDatabase()
        root = etree.fromstring(xml_content)

        # Parse channels
        channel_index = 1
        for ch_elem in root.findall("channel"):
            ch_id = ch_elem.get("id")
            if not ch_id:
                continue

            display_names = [d.text.strip() for d in ch_elem.findall("display-name") if d.text]
            call_sign = display_names[0] if display_names else ch_id
            name = display_names[1] if len(display_names) > 1 else call_sign

            # Extract major/minor channel numbers if available
            major = channel_index
            minor = 0
            for dn in display_names:
                match = re.search(r"\b(\d+)(?:\.(\d+))?\b", dn)
                if match:
                    major = int(match.group(1))
                    if match.group(2):
                        minor = int(match.group(2))
                    break

            # Check for channel logo icon
            icon_elem = ch_elem.find("icon")
            logo_url = icon_elem.get("src") if icon_elem is not None else None

            service_id = f"s_{ch_id.replace(' ', '_').replace('.', '_')}"
            channel = Channel(
                id=ch_id,
                service_id=service_id,
                call_sign=call_sign,
                name=name,
                number=major,
                sub_number=minor,
                logo_url=logo_url,
            )
            db.channels[ch_id] = channel
            channel_index += 1

        db.lineup = Lineup(channels=list(db.channels.values()))

        # Parse programmes
        prog_index = 1
        series_tracker = {}

        for p_elem in root.findall("programme"):
            ch_id = p_elem.get("channel")
            if not ch_id or ch_id not in db.channels:
                continue

            start_str = p_elem.get("start")
            stop_str = p_elem.get("stop")
            start_dt = parse_xmltv_date(start_str)
            stop_dt = parse_xmltv_date(stop_str)

            if not start_dt:
                continue

            duration_sec = 1800  # Default 30 mins
            if stop_dt and stop_dt > start_dt:
                duration_sec = int((stop_dt - start_dt).total_seconds())

            # Title & Sub-title
            title_elem = p_elem.find("title")
            title = title_elem.text.strip() if (title_elem is not None and title_elem.text) else "Untitled Program"

            sub_title_elem = p_elem.find("sub-title")
            episode_title = sub_title_elem.text.strip() if (sub_title_elem is not None and sub_title_elem.text) else None

            desc_elem = p_elem.find("desc")
            description = desc_elem.text.strip() if (desc_elem is not None and desc_elem.text) else None

            # Categories / Genres
            categories = [c.text.strip() for c in p_elem.findall("category") if c.text]
            cat_lower = [c.lower() for c in categories]

            is_movie = any(g in cat_lower for g in ["movie", "feature film", "cinema"])
            is_news = any(g in cat_lower for g in ["news", "current affairs"])
            is_sports = any(g in cat_lower for g in ["sports", "football", "baseball", "basketball", "soccer"])
            is_kids = any(g in cat_lower for g in ["children", "kids", "animation"])
            is_series = bool(episode_title) or (not is_movie and not is_news and not is_sports)

            season_num, episode_num = parse_episode_numbers(p_elem)
            if season_num or episode_num:
                is_series = True

            # Series linkage for WMC Series Recordings
            series_id = None
            if is_series:
                norm_title = re.sub(r"[^\w\s]", "", title).strip().lower()
                if norm_title not in series_tracker:
                    s_id = f"si_{len(series_tracker) + 1}"
                    db.series[s_id] = SeriesInfo(
                        id=s_id,
                        title=title,
                        short_title=title[:30],
                        description=description,
                    )
                    series_tracker[norm_title] = s_id
                series_id = series_tracker[norm_title]

            # Poster icon
            icon_elem = p_elem.find("icon")
            image_url = icon_elem.get("src") if icon_elem is not None else None

            # Rating
            rating_elem = p_elem.find(".//rating/value")
            rating_val = rating_elem.text.strip() if (rating_elem is not None and rating_elem.text) else None

            # Star rating
            star_rating = None
            half_stars = None
            star_elem = p_elem.find(".//star-rating/value")
            if star_elem is not None and star_elem.text:
                m = re.match(r"^([\d\.]+)(?:/(\d+))?", star_elem.text.strip())
                if m:
                    num = float(m.group(1))
                    denom = float(m.group(2)) if m.group(2) else 4.0
                    normalized_4 = (num / denom) * 4.0
                    star_rating = round(normalized_4, 1)
                    half_stars = int(round(normalized_4 * 2))

            # Technical flags
            is_repeat = p_elem.find("previously-shown") is not None
            video_elem = p_elem.find(".//video/quality")
            is_hdtv = True
            if video_elem is not None and video_elem.text:
                is_hdtv = "hd" in video_elem.text.lower()

            subtitles = p_elem.find("subtitles") is not None

            # Unique deterministic program key
            prog_key = f"{title}_{episode_title or ''}_{season_num or ''}_{episode_num or ''}"
            prog_id = f"pr_{prog_index}"
            prog_index += 1

            program = Program(
                id=prog_id,
                title=title,
                episode_title=episode_title,
                description=description,
                short_description=description[:100] if description else None,
                season_number=season_num,
                episode_number=episode_num,
                is_series=is_series,
                is_movie=is_movie,
                is_news=is_news,
                is_sports=is_sports,
                is_kids=is_kids,
                series_id=series_id,
                genres=categories,
                mpaa_rating=rating_val,
                star_rating=star_rating,
                half_stars_rating=half_stars,
                image_url=image_url,
            )
            db.programs[prog_id] = program

            # Schedule Entry
            channel_obj = db.channels[ch_id]
            sched_entry = ScheduleEntry(
                program_id=prog_id,
                service_id=channel_obj.service_id,
                start_time=start_dt,
                duration_seconds=duration_sec,
                is_hdtv=is_hdtv,
                is_repeat=is_repeat,
                is_stereo=True,
                is_cc=subtitles,
                is_live=False,
            )
            db.schedule_entries.append(sched_entry)

        logger.info(
            f"Parsed XMLTV data: {len(db.channels)} channels, "
            f"{len(db.programs)} programs, {len(db.schedule_entries)} schedule entries."
        )
        return db

