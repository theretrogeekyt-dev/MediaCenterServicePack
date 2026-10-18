import os
import tempfile
from pathlib import Path
import pytest

# Ensure clean test paths before importing settings
_temp_dir = tempfile.TemporaryDirectory()
os.environ["CONFIG_DIR"] = str(Path(_temp_dir.name) / "config")
os.environ["MEDIA_MOVIES_DIR"] = str(Path(_temp_dir.name) / "movies")
os.environ["MEDIA_MUSIC_DIR"] = str(Path(_temp_dir.name) / "music")


@pytest.fixture(scope="session")
def test_dirs():
    """Provides temporary workspace directories for testing."""
    config_dir = Path(os.environ["CONFIG_DIR"])
    movies_dir = Path(os.environ["MEDIA_MOVIES_DIR"])
    music_dir = Path(os.environ["MEDIA_MUSIC_DIR"])

    config_dir.mkdir(parents=True, exist_ok=True)
    movies_dir.mkdir(parents=True, exist_ok=True)
    music_dir.mkdir(parents=True, exist_ok=True)

    yield {
        "config": config_dir,
        "movies": movies_dir,
        "music": music_dir,
    }

    _temp_dir.cleanup()

