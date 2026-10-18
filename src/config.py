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

    def load_overrides(self):
        """Load persistent user overrides from /config/settings.json if present."""
        overrides_file = self.config_path / "settings.json"
        if overrides_file.exists():
            try:
                import json
                data = json.loads(overrides_file.read_text(encoding="utf-8"))
                for k, v in data.items():
                    if hasattr(self, k) and v is not None and v != "":
                        setattr(self, k, v)
            except Exception:
                pass

    def save_overrides(self, updates: dict):
        """Save persistent user overrides to /config/settings.json."""
        import json
        for k, v in updates.items():
            if hasattr(self, k) and v is not None:
                setattr(self, k, v)
        overrides_file = self.config_path / "settings.json"
        data = {
            "SCHEDULES_DIRECT_USER": self.SCHEDULES_DIRECT_USER,
            "SCHEDULES_DIRECT_PASS": self.SCHEDULES_DIRECT_PASS,
            "POSTAL_CODE": self.POSTAL_CODE,
            "EPG_API_KEY": self.EPG_API_KEY,
            "TMDB_API_KEY": self.TMDB_API_KEY,
        }
        overrides_file.write_text(json.dumps(data, indent=2), encoding="utf-8")


# Global settings singleton
settings = Settings()
settings.load_overrides()

