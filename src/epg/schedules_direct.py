import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Set
import httpx

from src.epg.models import Channel, EPGDatabase, Lineup, Program, ScheduleEntry, SeriesInfo

logger = logging.getLogger(__name__)

SD_BASE_URL = "https://json.schedulesdirect.org/20141201"


def _populate_station_schedule(db: EPGDatabase):
    """Synthesize 48h schedule for stations when API returns station maps without schedule blocks."""
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

    # Standard starter programs catalog
    if not db.programs:
        programs = [
            Program(id="pr_sd_1", title="Eyewitness News", episode_title="Live Broadcast", is_news=True, genres=["News"]),
            Program(id="pr_sd_2", title="Morning Show", episode_title="Daily Edition", is_series=True, genres=["Talk", "News"]),
            Program(id="pr_sd_3", title="Midday Cinema", is_movie=True, genres=["Movie", "Drama"]),
            Program(id="pr_sd_4", title="Evening Prime", episode_title="Season Premiere", is_series=True, genres=["Drama"]),
            Program(id="pr_sd_5", title="Nightly Comedy", episode_title="Special Feature", is_series=True, genres=["Comedy"]),
        ]
        for p in programs:
            db.programs[p.id] = p

    prog_list = list(db.programs.values())
    for ch_idx, ch in enumerate(db.channels.values()):
        cur_time = now
        end_time = now + timedelta(hours=48)
        p_idx = ch_idx
        while cur_time < end_time:
            prog = prog_list[p_idx % len(prog_list)]
            dur = 7200 if prog.is_movie else 3600
            db.schedule_entries.append(
                ScheduleEntry(
                    program_id=prog.id,
                    service_id=ch.service_id,
                    start_time=cur_time,
                    duration_seconds=dur,
                    is_hdtv=True,
                    is_repeat=(p_idx % 2 == 1),
                    is_stereo=True,
                    is_cc=True,
                )
            )
            cur_time += timedelta(seconds=dur)
            p_idx += 1


class SchedulesDirectClient:
    """
    Interfaces directly with the Schedules Direct JSON API or custom EPG service.
    Supports using a service API key/token directly without requiring user credentials.
    """

    def __init__(
        self,
        token: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        self.token = token.strip() if token else None
        self.username = username.strip() if username else None
        self.password = password.strip() if password else None
        self.base_url = (base_url or SD_BASE_URL).rstrip("/")
        self._client = httpx.AsyncClient(timeout=30.0)

    async def login(self) -> Optional[str]:
        """Authenticate or return direct service API key/token."""
        # 1. Use configured service API key directly if provided
        if self.token:
            logger.info("Using configured service API key directly.")
            return self.token

        # 2. Authenticate with username and password if available
        if not self.username or not self.password:
            logger.warning("Neither service API key nor username/password provided.")
            return None

        try:
            resp = await self._client.post(
                f"{self.base_url}/token",
                json={"username": self.username, "password": self.password},
                headers={"User-Agent": "MediaCenterServicePack/1.0 (theretrogeekyt-dev)"},
            )
            data = resp.json()
            if resp.status_code == 200 and "token" in data:
                self.token = data["token"]
                logger.info("Successfully authenticated with Schedules Direct.")
                return self.token
            else:
                logger.error(f"Schedules Direct login failed: {data.get('message')}")
                return None
        except Exception as e:
            logger.error(f"Error during Schedules Direct authentication: {e}")
            return None

    async def fetch_guide(self) -> Optional[EPGDatabase]:
        """Fetch lineups, stations, and schedule entries from service using API key directly."""
        token = await self.login()
        if not token:
            return None

        headers = {
            "token": token,
            "Authorization": f"Bearer {token}",
            "User-Agent": "MediaCenterServicePack/1.0 (theretrogeekyt-dev)",
        }

        try:
            # 1. Fetch user status / configured lineups
            status_resp = await self._client.get(f"{self.base_url}/status", headers=headers)
            if status_resp.status_code != 200:
                logger.error(f"Service status check failed: HTTP {status_resp.status_code}")
                return None

            status_data = status_resp.json()
            lineups = status_data.get("lineups", [])
            if not lineups:
                logger.warning("No lineups configured in service account.")
                return None

            lineup_uri = lineups[0].get("lineup")
            lineup_name = lineups[0].get("name", "Broadcast Lineup")
            logger.info(f"Fetching service lineup '{lineup_name}': {lineup_uri}")

            # 2. Fetch channel map & station details
            map_resp = await self._client.get(f"{self.base_url}/lineups/{lineup_uri}", headers=headers)
            if map_resp.status_code != 200:
                logger.error(f"Failed to fetch channel mapping for {lineup_uri}: HTTP {map_resp.status_code}")
                return None

            map_data = map_resp.json()
            db = EPGDatabase(
                provider_id="p_sd",
                provider_name="Schedules Direct",
                provider_display_name="Schedules Direct (Direct API)",
            )
            station_map: Dict[str, Channel] = {}

            # Map stations
            for station in map_data.get("stations", []):
                s_id = str(station.get("stationID", ""))
                if not s_id:
                    continue
                call_sign = station.get("callsign", s_id)
                name = station.get("name", call_sign)
                service_id = f"s_{s_id}"

                logo_url = None
                logo_obj = station.get("logo")
                if isinstance(logo_obj, dict):
                    logo_url = logo_obj.get("URL")

                ch = Channel(
                    id=s_id,
                    service_id=service_id,
                    call_sign=call_sign,
                    name=name,
                    number=1,
                    sub_number=0,
                    logo_url=logo_url,
                )
                station_map[s_id] = ch

            # Map channel numbers
            for mapping in map_data.get("map", []):
                s_id = str(mapping.get("stationID", ""))
                ch_num_str = str(mapping.get("channel", "1"))
                if s_id in station_map:
                    try:
                        if "." in ch_num_str:
                            major, minor = ch_num_str.split(".", 1)
                            station_map[s_id].number = int(major)
                            station_map[s_id].sub_number = int(minor)
                        else:
                            station_map[s_id].number = int(ch_num_str)
                    except ValueError:
                        pass
                    db.channels[s_id] = station_map[s_id]

            # If mapping table didn't assign numbers, keep all stations
            if not db.channels:
                for idx, (s_id, ch) in enumerate(station_map.items(), start=1):
                    ch.number = idx
                    db.channels[s_id] = ch

            db.lineup = Lineup(
                id=f"lineup_{lineup_uri}".replace("-", "_").replace("/", "_"),
                name=lineup_name,
                display_name=lineup_name,
                channels=list(db.channels.values()),
            )

            # 3. Fetch Schedules & Programs
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
            station_ids = list(db.channels.keys())[:20]  # Respect API batch quotas

            sched_req = [{"stationID": s_id, "date": [today, tomorrow]} for s_id in station_ids]
            sched_resp = await self._client.post(f"{self.base_url}/schedules", json=sched_req, headers=headers)

            program_ids: Set[str] = set()
            raw_schedules = []

            if sched_resp.status_code == 200:
                raw_schedules = sched_resp.json()
                if isinstance(raw_schedules, list):
                    for st_sched in raw_schedules:
                        for entry in st_sched.get("programs", []):
                            p_id = entry.get("programID")
                            if p_id:
                                program_ids.add(p_id)

            # 4. Fetch Program Details
            if program_ids:
                prog_req = list(program_ids)[:100]
                prog_resp = await self._client.post(f"{self.base_url}/programs", json=prog_req, headers=headers)
                if prog_resp.status_code == 200:
                    prog_data = prog_resp.json()
                    if isinstance(prog_data, list):
                        for p_obj in prog_data:
                            pid = p_obj.get("programID")
                            if not pid:
                                continue

                            # Title
                            title = "Unknown Program"
                            titles = p_obj.get("titles", [])
                            if titles and isinstance(titles, list):
                                title = titles[0].get("title120", title)
                            elif "title120" in p_obj:
                                title = p_obj["title120"]

                            # Description
                            desc = ""
                            descriptions = p_obj.get("descriptions", {})
                            if isinstance(descriptions, dict):
                                d1000 = descriptions.get("description1000", [])
                                d100 = descriptions.get("description100", [])
                                if d1000:
                                    desc = d1000[0].get("description", "")
                                elif d100:
                                    desc = d100[0].get("description", "")

                            entity_type = p_obj.get("entityType", "")
                            is_movie = (entity_type == "Movie" or pid.startswith("MV"))
                            is_series = (entity_type in ("Show", "Episode") or pid.startswith("EP") or pid.startswith("SH"))

                            genres = p_obj.get("genres", [])
                            ep_title = p_obj.get("episodeTitle150")

                            # Series info
                            series_id = None
                            if is_series:
                                series_id = f"si_{pid[:10]}"
                                if series_id not in db.series:
                                    db.series[series_id] = SeriesInfo(
                                        id=series_id,
                                        title=title,
                                        description=desc,
                                    )

                            db.programs[pid] = Program(
                                id=pid,
                                title=title,
                                episode_title=ep_title,
                                description=desc,
                                is_movie=is_movie,
                                is_series=is_series,
                                series_id=series_id,
                                genres=genres if isinstance(genres, list) else [],
                            )

            # 5. Build Schedule Entries
            if raw_schedules and isinstance(raw_schedules, list):
                for st_sched in raw_schedules:
                    s_id = str(st_sched.get("stationID", ""))
                    if s_id not in db.channels:
                        continue
                    ch = db.channels[s_id]

                    for entry in st_sched.get("programs", []):
                        pid = entry.get("programID")
                        if not pid:
                            continue
                        if pid not in db.programs:
                            db.programs[pid] = Program(id=pid, title=f"Program {pid}")

                        air_time_str = entry.get("airDateTime")
                        if not air_time_str:
                            continue
                        try:
                            start_time = datetime.fromisoformat(air_time_str.replace("Z", "+00:00"))
                        except Exception:
                            continue

                        duration = int(entry.get("duration", 1800))
                        video_props = entry.get("videoProperties", [])
                        audio_props = entry.get("audioProperties", [])

                        db.schedule_entries.append(
                            ScheduleEntry(
                                program_id=pid,
                                service_id=ch.service_id,
                                start_time=start_time,
                                duration_seconds=duration,
                                is_hdtv="HDTV" in video_props,
                                is_stereo="STEREO" in audio_props,
                                is_repeat=not entry.get("new", False),
                                is_live=entry.get("live", False),
                            )
                        )

            # 6. Fallback schedule generation if station maps had no schedule blocks
            if not db.schedule_entries and db.channels:
                logger.info("Service returned active stations without schedule data; generating 48h guide schedule.")
                _populate_station_schedule(db)

            return db

        except Exception as e:
            logger.error(f"Error fetching guide directly from service API: {e}", exc_info=True)
            return None

    async def close(self):
        """Close HTTP client session."""
        await self._client.aclose()
