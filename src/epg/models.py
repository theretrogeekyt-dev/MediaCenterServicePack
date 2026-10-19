from datetime import datetime
from typing import Dict, List, Optional
from pydantic import BaseModel, Field


class Channel(BaseModel):
    """TV Broadcast Channel entity."""
    id: str
    service_id: str
    call_sign: str
    name: str
    number: int = 1
    sub_number: int = 0
    logo_url: Optional[str] = None


class SeriesInfo(BaseModel):
    """Episodic series grouping entity."""
    id: str
    title: str
    short_title: Optional[str] = None
    description: Optional[str] = None
    short_description: Optional[str] = None
    start_air_date: Optional[str] = None
    image_url: Optional[str] = None


class Program(BaseModel):
    """Individual TV show, movie, or episode entity."""
    id: str
    title: str
    episode_title: Optional[str] = None
    description: Optional[str] = None
    short_description: Optional[str] = None
    language: str = "en"
    original_air_date: Optional[str] = None
    year: Optional[int] = None
    season_number: Optional[int] = None
    episode_number: Optional[int] = None
    is_series: bool = False
    is_movie: bool = False
    is_news: bool = False
    is_sports: bool = False
    is_kids: bool = False
    series_id: Optional[str] = None
    genres: List[str] = Field(default_factory=list)
    mpaa_rating: Optional[str] = None
    star_rating: Optional[float] = None  # 0.0 to 4.0
    half_stars_rating: Optional[int] = None  # 0 to 8
    image_url: Optional[str] = None


class ScheduleEntry(BaseModel):
    """Time-slot broadcast instance linking a Program to a Channel Service."""
    program_id: str
    service_id: str
    start_time: datetime
    duration_seconds: int
    is_hdtv: bool = True
    is_repeat: bool = False
    is_stereo: bool = True
    is_cc: bool = True
    is_live: bool = False
    audio_format: int = 2  # 2 = Stereo, 3 = Dolby Digital


class Lineup(BaseModel):
    """Collection of channel mappings."""
    id: str = "l1"
    uid: str = "!MCLineup!USA-OTA-67147"
    name: str = "Local Broadcast Listings"
    display_name: str = "Project eHome Lineup"
    postal_code: str = "67147"
    country: str = "US"
    channels: List[Channel] = Field(default_factory=list)


class EPGDatabase(BaseModel):
    """In-memory aggregated EPG database ready for MXF serialization."""
    provider_id: str = "p1"
    provider_name: str = "Project eHome"
    provider_display_name: str = "OpenMCE Guide Provider"
    channels: Dict[str, Channel] = Field(default_factory=dict)
    series: Dict[str, SeriesInfo] = Field(default_factory=dict)
    programs: Dict[str, Program] = Field(default_factory=dict)
    schedule_entries: List[ScheduleEntry] = Field(default_factory=list)
    lineup: Lineup = Field(default_factory=Lineup)

