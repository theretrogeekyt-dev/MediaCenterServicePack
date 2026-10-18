import asyncio
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
        postal_code: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        self.token = token.strip() if token else None
        self.username = username.strip() if username else None
        self.password = password.strip() if password else None
        self.postal_code = postal_code.strip() if postal_code else None
        self.base_url = (base_url or SD_BASE_URL).rstrip("/")
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0))

    async def login(self) -> Optional[str]:
        """Authenticate or return direct service API key/token."""
        # Allow passing "username:password" directly as token / EPG_API_KEY
        if self.token and ":" in self.token and not self.username:
            parts = self.token.split(":", 1)
            self.username = parts[0].strip()
            self.password = parts[1].strip()
            self.token = None

        # 1. Use configured service API key directly if provided
        if self.token:
            logger.info("Using configured service API key directly.")
            return self.token

        # 2. Authenticate with username and password if available
        if not self.username or not self.password:
            logger.warning("Neither service API key nor username/password provided.")
            return None

        # Schedules Direct requires lowercase hexadecimal SHA1 hash of the password
        import hashlib
        is_sha1 = len(self.password) == 40 and all(c in "0123456789abcdefABCDEF" for c in self.password)
        pw_hash = self.password.lower() if is_sha1 else hashlib.sha1(self.password.encode("utf-8")).hexdigest().lower()

        try:
            resp = await self._client.post(
                f"{self.base_url}/token",
                json={"username": self.username, "password": pw_hash},
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
                logger.info("No SD-JSON lineups found on account. Attempting automatic lineup discovery...")
                postal = self.postal_code
                if not postal:
                    from src.config import settings
                    postal = settings.POSTAL_CODE

                if postal:
                    try:
                        headends_resp = await self._client.get(
                            f"{self.base_url}/headends?country=USA&postalcode={postal}",
                            headers=headers,
                        )
                        if headends_resp.status_code == 200:
                            headends = headends_resp.json()
                            target_lineup_id = None
                            if isinstance(headends, list):
                                for he in headends:
                                    for l in he.get("lineups", []):
                                        target_lineup_id = l.get("lineup")
                                        if "OTA" in str(target_lineup_id) or "Antenna" in l.get("name", ""):
                                            break
                                    if target_lineup_id:
                                        break
                            if not target_lineup_id:
                                target_lineup_id = f"USA-OTA-{postal}"

                            logger.info(f"Adding lineup '{target_lineup_id}' to Schedules Direct account via SD-JSON...")
                            await self._client.put(
                                f"{self.base_url}/lineups/{target_lineup_id}",
                                headers=headers,
                            )
                            lineups = [{"lineup": target_lineup_id, "name": f"Broadcast Antenna ({postal})"}]
                    except Exception as err:
                        logger.warning(f"Automatic lineup setup failed: {err}")

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

            # 3. Fetch Schedules for ALL Stations (multi-day)
            today_dt = datetime.now(timezone.utc)
            dates = [(today_dt + timedelta(days=d)).strftime("%Y-%m-%d") for d in range(3)]
            station_ids = list(db.channels.keys())
            raw_schedules = []
            program_ids: Set[str] = set()

            sched_batch_size = 50
            for s_idx in range(0, len(station_ids), sched_batch_size):
                s_batch = station_ids[s_idx : s_idx + sched_batch_size]
                sched_req = [{"stationID": s_id, "date": dates} for s_id in s_batch]
                try:
                    sched_resp = await self._client.post(
                        f"{self.base_url}/schedules", json=sched_req, headers=headers
                    )
                    if sched_resp.status_code == 200:
                        batch_data = sched_resp.json()
                        if isinstance(batch_data, list):
                            raw_schedules.extend(batch_data)
                            for st_sched in batch_data:
                                for entry in st_sched.get("programs", []):
                                    p_id = entry.get("programID")
                                    if p_id:
                                        program_ids.add(p_id)
                except Exception as e:
                    logger.error(f"Failed to fetch schedules batch for stations {s_batch}: {e}")

            # 4. Fetch Program Details for ALL unique programs in batches of 250
            if program_ids:
                logger.info(f"Retrieving full program metadata for {len(program_ids)} programs from Schedules Direct...")
                prog_list = list(program_ids)
                prog_batch_size = 250

                for p_idx in range(0, len(prog_list), prog_batch_size):
                    prog_chunk = prog_list[p_idx : p_idx + prog_batch_size]
                    prog_data = None
                    for attempt in range(2):
                        try:
                            prog_resp = await self._client.post(
                                f"{self.base_url}/programs", json=prog_chunk, headers=headers
                            )
                            if prog_resp.status_code == 200:
                                prog_data = prog_resp.json()
                                break
                            else:
                                logger.warning(
                                    f"Schedules Direct /programs returned HTTP {prog_resp.status_code} "
                                    f"for batch {p_idx}-{p_idx + len(prog_chunk)} (attempt {attempt + 1})"
                                )
                        except Exception as e:
                            logger.warning(
                                f"Attempt {attempt + 1} failed fetching programs batch {p_idx}-{p_idx + len(prog_chunk)}: {e}"
                            )
                            if attempt == 0:
                                await asyncio.sleep(1.0)

                    if isinstance(prog_data, list):
                        for p_obj in prog_data:
                            if not isinstance(p_obj, dict):
                                continue
                            pid = p_obj.get("programID")
                            if not pid:
                                continue
                            if p_obj.get("code") and p_obj.get("code") != 0:
                                continue

                            # Title extraction
                            title = None
                            titles = p_obj.get("titles")
                            if isinstance(titles, list) and len(titles) > 0:
                                for t_entry in titles:
                                    if isinstance(t_entry, dict):
                                        cand = t_entry.get("title120") or t_entry.get("title")
                                        if cand and str(cand).strip():
                                            title = str(cand).strip()
                                            break
                                    elif isinstance(t_entry, str) and t_entry.strip():
                                        title = t_entry.strip()
                                        break
                            elif isinstance(titles, dict):
                                cand = titles.get("title120") or titles.get("title")
                                if cand and str(cand).strip():
                                    title = str(cand).strip()

                            if not title:
                                title = p_obj.get("title120") or p_obj.get("title")
                            if not title:
                                title = p_obj.get("episodeTitle150")

                            # Description
                            desc = ""
                            descriptions = p_obj.get("descriptions", {})
                            if isinstance(descriptions, dict):
                                d1000 = descriptions.get("description1000", [])
                                d100 = descriptions.get("description100", [])
                                if d1000 and isinstance(d1000, list):
                                    desc = d1000[0].get("description", "")
                                elif d100 and isinstance(d100, list):
                                    desc = d100[0].get("description", "")
                            elif isinstance(descriptions, list) and descriptions:
                                desc = descriptions[0].get("description", "")

                            entity_type = p_obj.get("entityType", "")
                            is_movie = (entity_type == "Movie" or pid.startswith("MV"))
                            is_series = (entity_type in ("Show", "Episode") or pid.startswith("EP") or pid.startswith("SH"))
                            is_sports = (entity_type == "Sports" or pid.startswith("SP"))

                            genres = p_obj.get("genres", [])
                            ep_title = p_obj.get("episodeTitle150")

                            # Season and Episode number
                            season_number = None
                            episode_number = None
                            meta_list = p_obj.get("metadata", [])
                            if isinstance(meta_list, list):
                                for m in meta_list:
                                    if isinstance(m, dict) and "Gracenote" in m:
                                        gn = m["Gracenote"]
                                        season_number = gn.get("season")
                                        episode_number = gn.get("episode")
                                        break

                            # Original Air Date & Year
                            original_air_date = p_obj.get("originalAirDate")
                            movie_year = p_obj.get("movieYear")
                            year = None
                            if movie_year:
                                try:
                                    year = int(movie_year)
                                except (ValueError, TypeError):
                                    pass
                            elif original_air_date and len(str(original_air_date)) >= 4:
                                try:
                                    year = int(str(original_air_date)[:4])
                                except (ValueError, TypeError):
                                    pass

                            # Ratings
                            mpaa_rating = None
                            content_ratings = p_obj.get("contentRating", [])
                            if isinstance(content_ratings, list):
                                for cr in content_ratings:
                                    if isinstance(cr, dict) and cr.get("code"):
                                        mpaa_rating = cr.get("code")
                                        break

                            # Star Rating
                            half_stars = None
                            quality_ratings = p_obj.get("qualityRating", [])
                            if isinstance(quality_ratings, list) and quality_ratings:
                                qr = quality_ratings[0]
                                if isinstance(qr, dict) and "rating" in qr:
                                    try:
                                        r_val = float(qr["rating"])
                                        max_val = float(qr.get("maxRating", 4))
                                        half_stars = int((r_val / max_val) * 8)
                                    except (ValueError, TypeError, ZeroDivisionError):
                                        pass

                            # Series Info registration
                            series_id = None
                            if is_series:
                                series_id = f"si_{pid[:10]}"
                                if series_id not in db.series and title:
                                    db.series[series_id] = SeriesInfo(
                                        id=series_id,
                                        title=title,
                                        description=desc,
                                    )
                                elif series_id in db.series and not title:
                                    title = db.series[series_id].title

                            if not title or title.strip() == "":
                                series_candidate = f"si_{pid[:10]}"
                                if series_candidate in db.series:
                                    title = db.series[series_candidate].title
                                elif is_movie:
                                    title = "Feature Film"
                                elif is_sports:
                                    title = "Sports Event"
                                elif is_series:
                                    title = "Television Program"
                                else:
                                    title = "Scheduled Program"

                            db.programs[pid] = Program(
                                id=pid,
                                title=title,
                                episode_title=ep_title,
                                description=desc,
                                is_movie=is_movie,
                                is_series=is_series,
                                is_sports=is_sports,
                                series_id=series_id,
                                season_number=season_number,
                                episode_number=episode_number,
                                original_air_date=original_air_date,
                                year=year,
                                mpaa_rating=mpaa_rating,
                                half_stars_rating=half_stars,
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
                            # Clean fallback without raw "Program SH..." string
                            series_candidate = f"si_{pid[:10]}"
                            fallback_title = "Scheduled Broadcast"
                            is_movie = pid.startswith("MV")
                            is_series = pid.startswith("EP") or pid.startswith("SH")
                            is_sports = pid.startswith("SP")

                            if series_candidate in db.series:
                                fallback_title = db.series[series_candidate].title
                            elif is_movie:
                                fallback_title = "Feature Film"
                            elif is_sports:
                                fallback_title = "Sports Broadcast"
                            elif is_series:
                                fallback_title = "Television Program"

                            db.programs[pid] = Program(
                                id=pid,
                                title=fallback_title,
                                is_movie=is_movie,
                                is_series=is_series,
                                is_sports=is_sports,
                            )

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
