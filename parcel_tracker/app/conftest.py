import os
import shutil
import tempfile

import pytest

# DATA_DIR must be set before any test module imports `db` (directly or via
# `main`), since db.py resolves it into a module-level path at import time.
# Setting it here, in a conftest.py, guarantees it happens before pytest
# imports any test_*.py file in this directory, regardless of import order.
_TMP_DATA_DIR = tempfile.mkdtemp(prefix="parcel-tracker-test-")
os.environ["DATA_DIR"] = _TMP_DATA_DIR
os.environ.setdefault("MAILBOXES_JSON", "[]")
os.environ.setdefault("SEVENTEENTRACK_API_KEY", "")
os.environ.setdefault("TRACK123_API_KEY", "")
os.environ.setdefault("SUPERVISOR_TOKEN", "")

# Same reasoning as DATA_DIR above: must be set before `main` is imported,
# since main.py resolves HA_WWW_DIR into a module-level path at import time.
_TMP_HA_WWW_DIR = tempfile.mkdtemp(prefix="parcel-tracker-test-www-")
os.environ["HA_WWW_DIR"] = _TMP_HA_WWW_DIR


@pytest.fixture(scope="session", autouse=True)
def _cleanup_data_dir():
    yield
    shutil.rmtree(_TMP_DATA_DIR, ignore_errors=True)
    shutil.rmtree(_TMP_HA_WWW_DIR, ignore_errors=True)
