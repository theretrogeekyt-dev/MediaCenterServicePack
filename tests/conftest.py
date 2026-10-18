import os
import tempfile
from pathlib import Path
import pytest

# Ensure clean test paths before importing settings
_temp_dir = tempfile.TemporaryDirectory()
os.environ["CONFIG_DIR"] = str(Path(_temp_dir.name) / "config")


@pytest.fixture(scope="session")
def test_dirs():
    """Provides temporary config directory for testing."""
    config_dir = Path(os.environ["CONFIG_DIR"])
    config_dir.mkdir(parents=True, exist_ok=True)

    yield {
        "config": config_dir,
    }

    _temp_dir.cleanup()
