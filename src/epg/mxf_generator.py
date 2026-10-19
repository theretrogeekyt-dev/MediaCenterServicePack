import datetime
import logging
from typing import Dict, List, Optional, Set
from lxml import etree

from src.epg.models import EPGDatabase, Program, ScheduleEntry

logger = logging.getLogger(__name__)

# Standard WMC Category Keywords
DEFAULT_KEYWORDS = {
    "k1": "Movie",
    "k2": "News",
    "k3": "Sports",
    "k4": "Comedy",
    "k5": "Drama",
    "k6": "Educational",
    "k7": "Children",
    "k8": "Science",
    "k9": "Action",
    "k10": "Talk",
    "k11": "Documentary",
    "k12": "Music",
}

# Standard WMC MPAA Ratings (Enum: 1=G, 2=PG, 3=PG13, 4=R, 5=NC17, 6=X, 7=NR, 8=AO)
MPAA_RATINGS = {
    "G": "1",
    "PG": "2",
    "PG-13": "3",
    "PG13": "3",
    "R": "4",
    "NC-17": "5",
    "NC17": "5",
    "X": "6",
    "NR": "7",
    "AO": "8",
}


def _bool_str(val: bool) -> str:
    """Format boolean for MXF XML attribute."""
    return "true" if val else "false"


class MXFGenerator:
    """Serializes an EPGDatabase object into schema-compliant Media Center XML Format (MXF)."""

    def __init__(self, db: EPGDatabase):
        self.db = db
        self._keyword_map: Dict[str, str] = {v.lower(): k for k, v in DEFAULT_KEYWORDS.items()}
        self._image_map: Dict[str, str] = {}  # url -> image_id

    def generate(self) -> bytes:
        """Construct the complete MXF XML document and return UTF-8 encoded bytes."""
        root = etree.Element("MXF")

        # 1. Assembly & Namespace Declarations (omitting fixed version allows universal loading across Win7/8/10/11)
        mcepg_asm = etree.SubElement(
            root,
            "Assembly",
            name="mcepg",
        )
        mcepg_ns = etree.SubElement(mcepg_asm, "NameSpace", name="Microsoft.MediaCenter.Guide")
        etree.SubElement(mcepg_ns, "Type", name="Lineup")
        etree.SubElement(mcepg_ns, "Type", name="Channel", parentFieldName="lineup")
        etree.SubElement(mcepg_ns, "Type", name="Service")
        etree.SubElement(mcepg_ns, "Type", name="ScheduleEntry", groupName="ScheduleEntries")
        etree.SubElement(mcepg_ns, "Type", name="Program")
        etree.SubElement(mcepg_ns, "Type", name="SeriesInfo")
        etree.SubElement(mcepg_ns, "Type", name="Keyword")
        etree.SubElement(mcepg_ns, "Type", name="KeywordGroup")
        etree.SubElement(mcepg_ns, "Type", name="GuideImage")

        mcstore_asm = etree.SubElement(
            root,
            "Assembly",
            name="mcstore",
        )
        mcstore_ns = etree.SubElement(mcstore_asm, "NameSpace", name="Microsoft.MediaCenter.Store")
        etree.SubElement(mcstore_ns, "Type", name="Provider")

        # 2. Providers block
        providers = etree.SubElement(root, "Providers")
        etree.SubElement(
            providers,
            "Provider",
            id=self.db.provider_id,
            name=self.db.provider_name,
            displayName=self.db.provider_display_name,
            copyright="OpenMCE Project eHome",
        )

        # 3. Scoped container <With provider="...">
        with_elem = etree.SubElement(root, "With", provider=self.db.provider_id)

        # Build Keywords
        self._build_keywords(with_elem)

        # Build Guide Images (collect all URLs from channels and programs)
        self._build_guide_images(with_elem)

        # Build SeriesInfos
        self._build_series_infos(with_elem)

        # Build Programs
        self._build_programs(with_elem)

        # Build Services
        self._build_services(with_elem)

        # Build ScheduleEntries grouped by service
        self._build_schedule_entries(with_elem)

        # Build Lineups & Channels
        self._build_lineups(with_elem)

        # Return formatted XML bytes
        return etree.tostring(
            root,
            xml_declaration=True,
            encoding="utf-8",
            pretty_print=True,
        )

    def _build_keywords(self, parent: etree._Element):
        keywords_elem = etree.SubElement(parent, "Keywords")
        for k_id, word in DEFAULT_KEYWORDS.items():
            etree.SubElement(keywords_elem, "Keyword", id=k_id, word=word)

        # Keyword Group 1 (Genres)
        kg_elem = etree.SubElement(parent, "KeywordGroups")
        all_kw_ids = ",".join(DEFAULT_KEYWORDS.keys())
        etree.SubElement(kg_elem, "KeywordGroup", group="1", keywords=all_kw_ids)

    def _build_guide_images(self, parent: etree._Element):
        images_elem = etree.SubElement(parent, "GuideImages")
        img_idx = 1

        # Collect channel logos
        for ch in self.db.channels.values():
            if ch.logo_url and ch.logo_url not in self._image_map:
                img_id = f"gi_{img_idx}"
                self._image_map[ch.logo_url] = img_id
                img_idx += 1
                etree.SubElement(images_elem, "GuideImage", id=img_id, imageUrl=ch.logo_url)

        # Collect program posters
        for pr in self.db.programs.values():
            if pr.image_url and pr.image_url not in self._image_map:
                img_id = f"gi_{img_idx}"
                self._image_map[pr.image_url] = img_id
                img_idx += 1
                etree.SubElement(images_elem, "GuideImage", id=img_id, imageUrl=pr.image_url)

    def _build_series_infos(self, parent: etree._Element):
        series_elem = etree.SubElement(parent, "SeriesInfos")
        for s_id, s_info in self.db.series.items():
            attribs = {
                "id": s_id,
                "uid": f"!Series!{s_id}",
                "title": s_info.title,
            }
            if s_info.short_title:
                attribs["shortTitle"] = s_info.short_title
            if s_info.description:
                attribs["description"] = s_info.description
            if s_info.start_air_date:
                attribs["startAirdate"] = s_info.start_air_date
            etree.SubElement(series_elem, "SeriesInfo", **attribs)

    def _build_programs(self, parent: etree._Element):
        programs_elem = etree.SubElement(parent, "Programs")
        for pr_id, pr in self.db.programs.items():
            attribs = {
                "id": pr_id,
                "uid": f"!Program!{pr_id}",
                "title": pr.title,
                "language": pr.language or "en",
                "isSeries": _bool_str(pr.is_series),
                "isMovie": _bool_str(pr.is_movie),
            }
            if pr.episode_title:
                attribs["episodeTitle"] = pr.episode_title
            if pr.description:
                attribs["description"] = pr.description
            if pr.short_description:
                attribs["shortDescription"] = pr.short_description
            if pr.original_air_date:
                attribs["originalAirdate"] = pr.original_air_date
            if pr.year:
                attribs["year"] = str(pr.year)
            if pr.season_number is not None:
                attribs["seasonNumber"] = str(pr.season_number)
            if pr.episode_number is not None:
                attribs["episodeNumber"] = str(pr.episode_number)
            if pr.series_id:
                attribs["series"] = pr.series_id
            if pr.is_news:
                attribs["isNews"] = "true"
            if pr.is_sports:
                attribs["isSports"] = "true"
            if pr.is_kids:
                attribs["isKids"] = "true"
            if pr.mpaa_rating:
                cleaned_mpaa = pr.mpaa_rating.strip().upper().replace(" ", "")
                if cleaned_mpaa in MPAA_RATINGS:
                    attribs["mpaaRating"] = MPAA_RATINGS[cleaned_mpaa]
            if pr.half_stars_rating is not None:
                attribs["halfStars"] = str(pr.half_stars_rating)

            # Map genres to keywords
            matched_keywords = []
            for genre in pr.genres:
                g_low = genre.lower()
                if g_low in self._keyword_map:
                    matched_keywords.append(self._keyword_map[g_low])
            if matched_keywords:
                attribs["keywords"] = ",".join(matched_keywords)

            # Link poster image
            if pr.image_url and pr.image_url in self._image_map:
                attribs["guideImage"] = self._image_map[pr.image_url]

            etree.SubElement(programs_elem, "Program", **attribs)

    def _build_services(self, parent: etree._Element):
        services_elem = etree.SubElement(parent, "Services")
        for ch in self.db.channels.values():
            attribs = {
                "id": ch.service_id,
                "uid": f"!Service!{ch.id}",
                "name": ch.name,
                "callSign": ch.call_sign,
            }
            if ch.logo_url and ch.logo_url in self._image_map:
                attribs["logoImage"] = self._image_map[ch.logo_url]

            etree.SubElement(services_elem, "Service", **attribs)

    def _build_schedule_entries(self, parent: etree._Element):
        # Group schedule entries by service_id
        grouped: Dict[str, List[ScheduleEntry]] = {}
        for entry in self.db.schedule_entries:
            grouped.setdefault(entry.service_id, []).append(entry)

        for s_id, entries in grouped.items():
            se_elem = etree.SubElement(parent, "ScheduleEntries", service=s_id)
            # Sort chronologically
            entries.sort(key=lambda e: e.start_time)
            for entry in entries:
                start_iso = entry.start_time.strftime("%Y-%m-%dT%H:%M:%SZ")
                end_time = entry.start_time + datetime.timedelta(seconds=entry.duration_seconds)
                end_iso = end_time.strftime("%Y-%m-%dT%H:%M:%SZ")
                attribs = {
                    "program": entry.program_id,
                    "startTime": start_iso,
                    "duration": str(entry.duration_seconds),
                    "endTime": end_iso,
                    "isHdtv": _bool_str(entry.is_hdtv),
                    "isRepeat": _bool_str(entry.is_repeat),
                    "isStereo": _bool_str(entry.is_stereo),
                    "isCc": _bool_str(entry.is_cc),
                }
                if entry.is_live:
                    attribs["isLive"] = "true"
                if entry.audio_format != 2:
                    attribs["audioFormat"] = str(entry.audio_format)

                etree.SubElement(se_elem, "ScheduleEntry", **attribs)

    def _build_lineups(self, parent: etree._Element):
        lineups_elem = etree.SubElement(parent, "Lineups")
        lineup_attribs = {
            "id": self.db.lineup.id,
            "uid": self.db.lineup.uid,
            "name": self.db.lineup.name,
            "lineupTypes": "ATSC;Antenna;TER;Cable",
        }
        lineup_elem = etree.SubElement(lineups_elem, "Lineup", **lineup_attribs)
        channels_elem = etree.SubElement(lineup_elem, "channels")

        ch_idx = 1
        for ch in self.db.channels.values():
            c_id = f"c_{ch_idx}"
            ch_idx += 1
            c_attribs = {
                "id": c_id,
                "lineup": self.db.lineup.id,
                "service": ch.service_id,
                "number": str(ch.number),
                "subNumber": str(ch.sub_number),
                "matchName": ch.call_sign,
            }
            etree.SubElement(channels_elem, "Channel", **c_attribs)

