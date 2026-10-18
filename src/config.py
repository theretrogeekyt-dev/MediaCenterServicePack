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

    # DNS Auto-Discovery & Domain Redirection Server
    ENABLE_DNS_SERVER: bool = True
    DNS_PORT: int = 53
    DNS_HOST_IP: Optional[str] = None
    UPSTREAM_DNS: str = "1.1.1.1,8.8.8.8"

    # Public DNS & WAN Auto-Discovery Configuration
    DNS_PUBLIC_IP: Optional[str] = None
    DNS_AUTO_DETECT_PUBLIC_IP: bool = True
    DNS_AUTHORITATIVE_ONLY: bool = False
    DNS_ALLOW_EXTERNAL_RECURSION: bool = False
    DNS_RATE_LIMIT_PER_SEC: int = 50

    # Internal cache for detected public WAN IP
    _cached_public_ip: Optional[str] = None
    _cached_public_ip_time: float = 0.0

    @property
    def detected_host_ip(self) -> str:
        """Returns configured DNS_HOST_IP or auto-detects primary outgoing local IP address."""
        if self.DNS_HOST_IP and self.DNS_HOST_IP.strip():
            return self.DNS_HOST_IP.strip()
        import socket
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("1.1.1.1", 80))
                return s.getsockname()[0]
        except Exception:
            return "127.0.0.1"

    @property
    def public_host_ip(self) -> str:
        """
        Returns the public IP or resolved DDNS hostname for external clients.
        If DNS_PUBLIC_IP is set, returns it (resolving DDNS hostname if necessary).
        If DNS_AUTO_DETECT_PUBLIC_IP is True, fetches and caches public WAN IP.
        Falls back to detected_host_ip.
        """
        if self.DNS_PUBLIC_IP and self.DNS_PUBLIC_IP.strip():
            raw = self.DNS_PUBLIC_IP.strip()
            import ipaddress
            try:
                ipaddress.ip_address(raw)
                return raw
            except ValueError:
                import socket
                try:
                    return socket.gethostbyname(raw)
                except Exception:
                    return raw

        if self.DNS_AUTO_DETECT_PUBLIC_IP:
            import time
            now = time.time()
            if self._cached_public_ip and (now - self._cached_public_ip_time < 3600):
                return self._cached_public_ip
            ip = self.fetch_external_ip()
            if ip:
                self._cached_public_ip = ip
                self._cached_public_ip_time = now
                return ip

        return self.detected_host_ip

    @staticmethod
    def fetch_external_ip() -> Optional[str]:
        """Fetch the public WAN IP via external HTTP lookup service with timeout."""
        import urllib.request
        import ipaddress
        endpoints = [
            "https://api.ipify.org",
            "https://icanhazip.com",
            "https://ifconfig.me/ip",
        ]
        for url in endpoints:
            try:
                req = urllib.request.Request(
                    url,
                    headers={"User-Agent": "MediaCenterServicePack/1.0"}
                )
                with urllib.request.urlopen(req, timeout=2.0) as resp:
                    ip_str = resp.read().decode("utf-8").strip()
                    if ip_str:
                        parsed = ipaddress.ip_address(ip_str)
                        if parsed.version == 4:
                            return ip_str
            except Exception:
                continue
        return None

    @property
    def epg_service_key(self) -> Optional[str]:
        """Returns direct service API key if configured (EPG_API_KEY or SCHEDULES_DIRECT_KEY)."""
        return self.EPG_API_KEY or self.SCHEDULES_DIRECT_KEY

    @property
    def config_path(self) -> Path:
        """Resolved path to the config directory."""
        path = Path(self.CONFIG_DIR)
        try:
            path.mkdir(parents=True, exist_ok=True)
        except (PermissionError, OSError):
            pass
        return path

    @property
    def guide_mxf_file(self) -> Path:
        """Resolved path to the primary guide.mxf file."""
        return self.config_path / "guide.mxf"

    @property
    def fallback_guide_mxf_file(self) -> Path:
        """Guaranteed writable fallback path if the volume mount has strict permissions."""
        import tempfile
        fallback_dir = Path(tempfile.gettempdir()) / "mediacenter_config"
        try:
            fallback_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return fallback_dir / "guide.mxf"

    def get_existing_guide_mxf(self) -> Optional[Path]:
        """Check for existing guide.mxf, checking primary volume then fallback."""
        try:
            if self.guide_mxf_file.exists() and self.guide_mxf_file.stat().st_size > 0:
                return self.guide_mxf_file
        except (PermissionError, OSError):
            pass
        try:
            if self.fallback_guide_mxf_file.exists() and self.fallback_guide_mxf_file.stat().st_size > 0:
                return self.fallback_guide_mxf_file
        except Exception:
            pass
        return None

    def load_overrides(self):
        """Load persistent user overrides from settings.json if present."""
        candidates = [
            self.config_path / "settings.json",
            self.fallback_guide_mxf_file.parent / "settings.json",
        ]
        for overrides_file in candidates:
            try:
                if overrides_file.exists():
                    import json
                    data = json.loads(overrides_file.read_text(encoding="utf-8"))
                    for k, v in data.items():
                        if hasattr(self, k) and v is not None and v != "":
                            setattr(self, k, v)
                    break
            except Exception:
                pass

    def save_overrides(self, updates: dict):
        """Save persistent user overrides to settings.json, handling permission issues."""
        import json
        for k, v in updates.items():
            if hasattr(self, k) and v is not None:
                setattr(self, k, v)
        data = {
            "SCHEDULES_DIRECT_USER": self.SCHEDULES_DIRECT_USER,
            "SCHEDULES_DIRECT_PASS": self.SCHEDULES_DIRECT_PASS,
            "POSTAL_CODE": self.POSTAL_CODE,
            "EPG_API_KEY": self.EPG_API_KEY,
            "TMDB_API_KEY": self.TMDB_API_KEY,
            "DNS_PUBLIC_IP": self.DNS_PUBLIC_IP,
            "DNS_AUTO_DETECT_PUBLIC_IP": self.DNS_AUTO_DETECT_PUBLIC_IP,
            "DNS_AUTHORITATIVE_ONLY": self.DNS_AUTHORITATIVE_ONLY,
            "DNS_ALLOW_EXTERNAL_RECURSION": self.DNS_ALLOW_EXTERNAL_RECURSION,
            "DNS_RATE_LIMIT_PER_SEC": self.DNS_RATE_LIMIT_PER_SEC,
        }
        content = json.dumps(data, indent=2)

        # Attempt to write to primary config path
        try:
            overrides_file = self.config_path / "settings.json"
            overrides_file.parent.mkdir(parents=True, exist_ok=True)
            if overrides_file.exists():
                try:
                    overrides_file.chmod(0o666)
                except Exception:
                    pass
            overrides_file.write_text(content, encoding="utf-8")
        except (PermissionError, OSError):
            # Fallback to writable directory
            try:
                fallback_file = self.fallback_guide_mxf_file.parent / "settings.json"
                fallback_file.parent.mkdir(parents=True, exist_ok=True)
                fallback_file.write_text(content, encoding="utf-8")
            except Exception:
                pass


# Global settings singleton
settings = Settings()
settings.load_overrides()

