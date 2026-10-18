from pathlib import Path
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings for MediaCenterServicePack."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Server configuration
    HOST: str = "0.0.0.0"
    PORT: int = 8080
    DEBUG: bool = False
    TZ: str = "UTC"

    # User / Group mapping (NAS permissions)
    PUID: int = 1000
    PGID: int = 1000

    # Paths
    CONFIG_DIR: str = "/config"

    # Upstream Guide sources
    EPG_API_KEY: Optional[str] = None
    SCHEDULES_DIRECT_KEY: Optional[str] = None
    SCHEDULES_DIRECT_USER: Optional[str] = None
    SCHEDULES_DIRECT_PASS: Optional[str] = None
    POSTAL_CODE: Optional[str] = None
    EPG_SERVICE_URL: Optional[str] = None
    XMLTV_URL: Optional[str] = None
    XMLTV_FILE: Optional[str] = None

    # Metadata API keys
    TMDB_API_KEY: Optional[str] = None

    # Cron schedules
    CRON_EPG_SYNC: str = "0 3 * * *"       # Daily at 03:00

    @property
    def epg_service_key(self) -> Optional[str]:
        """Returns direct service API key if configured (EPG_API_KEY or SCHEDULES_DIRECT_KEY)."""
        return self.EPG_API_KEY or self.SCHEDULES_DIRECT_KEY

    @property
    def config_path(self) -> Path:
        """Resolved path to the config directory."""
        path = Path(self.CONFIG_DIR)
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def guide_mxf_file(self) -> Path:
        """Resolved path to the compiled guide.mxf file."""
        return self.config_path / "guide.mxf"

    @property
    def cache_path(self) -> Path:
        """Resolved path to local cache directory."""
        cache_dir = self.config_path / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir


# Global settings singleton
settings = Settings()

