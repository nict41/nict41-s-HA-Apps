"""Add-on options and the storage path they resolve to.

Options arrive as environment variables exported by the run script. The one
that needs real work is `zim_path`: ZIM archives live on a network share
mapped into Home Assistant as Media storage, and a share can be unmounted,
renamed or read-only at any moment. Nothing here raises on a broken share -
`storage_status()` reports what is wrong and the rest of the app degrades to
a clear message instead of crashing.
"""

import os
import shutil
from pathlib import Path

# Where Home Assistant mounts Media-usage network storage. Overridable so the
# tests can point the whole app at a temporary directory.
MEDIA_ROOT = Path(os.environ.get("MEDIA_ROOT", "/media"))

# The add-on's own persistent storage - state only, never ZIM files.
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))

LIBRARY_SOURCE = os.environ.get("LIBRARY_SOURCE", "https://library.kiwix.org").rstrip("/")
CATALOG_LANGUAGE = os.environ.get("CATALOG_LANGUAGE", "eng").strip()
AUTO_SERVE_NEW = os.environ.get("AUTO_SERVE_NEW", "true").lower() == "true"
MAX_CONCURRENT_DOWNLOADS = max(1, min(3, int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "1") or 1)))

# The Home Assistant ingress prefix this add-on is reached through, e.g.
# /api/hassio_ingress/<token>. kiwix-serve needs it to build absolute links;
# see server.py. Empty when the add-on is opened on its direct port.
INGRESS_ENTRY = os.environ.get("INGRESS_ENTRY", "").rstrip("/")

# Port kiwix-serve listens on, loopback-only - it is never exposed directly,
# the manager proxies it under /kiwix so everything stays behind HA's auth.
KIWIX_PORT = int(os.environ.get("KIWIX_PORT", "8090"))

# Refuse to treat these as a ZIM directory: writing hundreds of gigabytes to
# the media root itself (or to /) would silently fill Home Assistant's own
# disk when what the user meant was a folder on their share.
_FORBIDDEN = {Path("/"), Path("/media"), Path("/data"), Path("/config"), Path("/share")}


def raw_zim_path() -> str:
    return os.environ.get("ZIM_PATH", "").strip()


def resolve_zim_path() -> Path | None:
    """The configured storage directory as an absolute path, or None if unset.

    A value starting with `/` is taken as-is; anything else is relative to
    /media, so both `NAS1/Kiwix` and `/media/NAS1/Kiwix` mean the same thing.
    """
    raw = raw_zim_path()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = MEDIA_ROOT / path
    return Path(os.path.normpath(path))


def _writable(path: Path) -> bool:
    """Whether a directory really accepts writes.

    os.access() alone lies on network shares - a CIFS mount can report the
    permission bits of a share exported read-only - so this actually creates
    and removes a probe file.
    """
    probe = path / ".kiwix-write-test"
    try:
        probe.touch()
        probe.unlink()
        return True
    except OSError:
        return False


def storage_status() -> dict:
    """What state the ZIM storage directory is in, as a UI-ready dict.

    `state` is one of: unset, forbidden, unmounted, missing, not_a_directory,
    read_only, ok. Everything but `ok` carries a message explaining what to
    fix, and the app keeps running either way.
    """
    path = resolve_zim_path()
    if path is None:
        return {
            "state": "unset",
            "path": "",
            "ok": False,
            "message": (
                "No ZIM storage path is configured. Set the add-on's "
                "'ZIM storage path' option to a folder on your network share "
                "(e.g. NAS1/Kiwix) and restart the add-on."
            ),
        }

    result = {"state": "ok", "path": str(path), "ok": True, "message": ""}

    if path in _FORBIDDEN:
        return {
            **result,
            "state": "forbidden",
            "ok": False,
            "message": (
                f"'{path}' is not a safe place to store ZIM files. Point the "
                "path at a folder on your network share, e.g. NAS1/Kiwix."
            ),
        }

    if not path.exists():
        # A missing parent means the share itself isn't there, which is a very
        # different problem from "the folder hasn't been created yet".
        if not path.parent.is_dir():
            return {
                **result,
                "state": "unmounted",
                "ok": False,
                "message": (
                    f"'{path.parent}' doesn't exist, so the network share "
                    "looks like it isn't mounted. Check Settings -> System -> "
                    "Storage in Home Assistant, then reload this page - no "
                    "need to restart the add-on."
                ),
            }
        try:
            path.mkdir(parents=False, exist_ok=True)
        except OSError as exc:
            return {
                **result,
                "state": "missing",
                "ok": False,
                "message": f"Could not create '{path}': {exc}",
            }

    if not path.is_dir():
        return {
            **result,
            "state": "not_a_directory",
            "ok": False,
            "message": f"'{path}' exists but is not a directory.",
        }

    if not _writable(path):
        return {
            **result,
            "state": "read_only",
            "ok": False,
            "message": (
                f"'{path}' is not writable by the add-on. Downloads would "
                "fail; check the share's permissions and how it is mounted "
                "in Home Assistant."
            ),
        }

    try:
        usage = shutil.disk_usage(path)
        result["free_bytes"] = usage.free
        result["total_bytes"] = usage.total
    except OSError:
        pass

    return result


def as_dict() -> dict:
    """The options, for display in the UI."""
    return {
        "zim_path": raw_zim_path(),
        "library_source": LIBRARY_SOURCE,
        "catalog_language": CATALOG_LANGUAGE,
        "auto_serve_new": AUTO_SERVE_NEW,
        "max_concurrent_downloads": MAX_CONCURRENT_DOWNLOADS,
    }
