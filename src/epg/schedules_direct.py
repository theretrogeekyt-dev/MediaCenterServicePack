import logging
from datetime import datetime, timezone
from typing import Dict, Optional
import httpx

from src.epg.models import Channel, EPGDatabase, Lineup, Program, ScheduleEntry, SeriesInfo

logger = logging.getLogger(__name__)

SD_BASE_URL = "https://json.schedulesdirect.org/20141201"


class SchedulesDirectClient:
    """Interfaces with the Schedules Direct JSON API."""

    def __init__(self, username: Optional[str] = None, password: Optional[str] = None, token: Optional[str] = None):
        self.username = username
        self.password = password
        self.token = token
        self._client = httpx.AsyncClient(timeout=30.0)

    async def login(self) -> Optional[str]:
        """Authenticate with Schedules Direct and obtain a session token."""
        if self.token:
            return self.token

        if not self.username or not self.password:
            logger.warning("Schedules Direct credentials not provided.")
            return None

        try:
            resp = await self._client.post(
                f"{SD_BASE_URL}/token",
                json={"username": self.username, "password": self.password},
                headers={"User-Agent": "Project-eHome/1.0 (OpenMCE)"},
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
        """Fetch lineups, stations, and schedule entries from Schedules Direct."""
        token = await self.login()
        if not token:
            return None

        headers = {
            "token": token,
            "User-Agent": "Project-eHome/1.0 (OpenMCE)",
        }

        try:
            # 1. Fetch user status / lineups
            status_resp = await self._client.get(f"{SD_BASE_URL}/status", headers=headers)
            if status_resp.status_code != 200:
                logger.error(f"Failed to fetch Schedules Direct status: {status_resp.status_code}")
                return None

            status_data = status_resp.json()
            lineups = status_data.get("lineups", [])
            if not lineups:
                logger.warning("No lineups configured in Schedules Direct account.")
                return None

            lineup_uri = lineups[0].get("lineup")
            logger.info(f"Fetching lineup: {lineup_uri}")

            # 2. Fetch channel map
            map_resp = await self._client.get(f"{SD_BASE_URL}/lineups/{lineup_uri}", headers=headers)
            if map_resp.status_code != 200:
                logger.error(f"Failed to fetch channel mapping for {lineup_uri}")
                return None

            map_data = map_resp.json()
            db = EPGDatabase()
            station_map: Dict[str, Channel] = {}

            # Map stations
            for station in map_data.get("stations", []):
                s_id = station.get("stationID")
                call_sign = station.get("callsign", s_id)
                name = station.get("name", call_sign)
                service_id = f"s_{s_id}"

                ch = Channel(
                    id=s_id,
                    service_id=service_id,
                    call_sign=call_sign,
                    name=name,
                    number=1,
                    sub_number=0,
                )
                station_map[s_id] = ch

            # Map channel numbers
            for mapping in map_data.get("map", []):
                s_id = mapping.get("stationID")
                ch_num_str = mapping.get("channel", "1")
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

            db.lineup = Lineup(channels=list(db.channels.values()))
            return db

        except Exception as e:
            logger.error(f"Error fetching guide from Schedules Direct: {e}")
            return None

    async def close(self):
        await self._client.aclose()

