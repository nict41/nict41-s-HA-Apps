"""Test fixtures.

Every test runs against a throwaway SQLite file and never makes a real
network call - Open Food Facts, the vision model, and the Home Assistant
Supervisor API are all monkeypatched or stubbed per test.
"""

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(__file__))

# Read before any module captures them: main.py calls db.init_db() at import
# time (outside any fixture), so DATA_DIR must already point somewhere
# writable and test-only *before* that first `import main` happens - which
# for test_main.py occurs at collection time, before any per-test fixture
# below has had a chance to run.
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="pantry_tracker_test_"))
os.environ.setdefault("SUPERVISOR_TOKEN", "")


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """A fresh data directory and a clean slate of env-sourced settings for
    every test, so tests never share state or touch the real add-on
    configuration."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SUPERVISOR_TOKEN", "")
    monkeypatch.setenv("NOTIFY_SERVICE", "")
    monkeypatch.setenv("NOTIFY_DAYS_BEFORE", "3")
    monkeypatch.setenv("NOTIFY_REPEAT_HOURS", "24")
    monkeypatch.setenv("CHECK_INTERVAL_HOURS", "12")
    monkeypatch.setenv("VISION_API_BASE", "")
    monkeypatch.setenv("VISION_API_KEY", "")
    monkeypatch.setenv("VISION_MODEL", "qwen2.5vl:3b")

    import db

    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "pantry.db")
    db.init_db()
    yield
