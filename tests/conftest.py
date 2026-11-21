"""Shared test setup.

Environment is fixed before any application module is imported, because
config.Settings reads it once at import time.
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["SQLITE_PATH"] = os.path.join(tempfile.mkdtemp(), "import-time.db")
os.environ["OFFSET_API_KEY"] = "test-admin-key"
os.environ["TDP_CPU_WATTS"] = "50"
os.environ["REPORT_TIMEZONE"] = "America/New_York"
for name in ("ELECTRICITYMAP_API_KEY", "CNAUGHT_API_KEY", "CLOUDFLARE_API_TOKEN", "WORKER_ORIGIN_SECRET"):
    os.environ.pop(name, None)

import pytest  # noqa: E402


@pytest.fixture
def fresh_db(tmp_path):
    """An empty, initialised database that the db module is pointed at."""
    import db
    from config import settings

    db.close_db()
    settings.sqlite_path = str(tmp_path / "carbon.db")
    db.init_db()
    yield db
    db.close_db()
