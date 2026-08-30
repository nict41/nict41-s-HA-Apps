import os
import shutil
import tempfile

import pytest

# settings.py resolves DATA_DIR and MEDIA_ROOT into module-level paths at
# import time, so both have to exist in the environment before pytest imports
# any test module (which in turn imports settings, directly or via main).
_TMP_DATA_DIR = tempfile.mkdtemp(prefix="kiwix-test-data-")
_TMP_MEDIA_ROOT = tempfile.mkdtemp(prefix="kiwix-test-media-")
os.environ["DATA_DIR"] = _TMP_DATA_DIR
os.environ["MEDIA_ROOT"] = _TMP_MEDIA_ROOT
os.environ.setdefault("ZIM_PATH", "")
os.environ.setdefault("INGRESS_ENTRY", "")


@pytest.fixture
def zim_dir(monkeypatch):
    """A configured, healthy storage directory on the fake media root."""
    path = os.path.join(_TMP_MEDIA_ROOT, "NAS1", "Kiwix")
    os.makedirs(path, exist_ok=True)
    monkeypatch.setenv("ZIM_PATH", "NAS1/Kiwix")
    yield path
    shutil.rmtree(os.path.join(_TMP_MEDIA_ROOT, "NAS1"), ignore_errors=True)


@pytest.fixture(autouse=True)
def _clean_state():
    """Each test starts with no served selection and no download jobs."""
    import downloads
    import library

    library.state_file().unlink(missing_ok=True)
    library.library_xml().unlink(missing_ok=True)
    with downloads._lock:
        downloads._jobs.clear()
    yield


@pytest.fixture(scope="session", autouse=True)
def _cleanup_dirs():
    yield
    shutil.rmtree(_TMP_DATA_DIR, ignore_errors=True)
    shutil.rmtree(_TMP_MEDIA_ROOT, ignore_errors=True)
