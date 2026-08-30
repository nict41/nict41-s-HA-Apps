"""Add-on options, the runtime settings layered over them, and the storage
path they resolve to.

Options arrive as environment variables exported by the run script. They are
only ever *defaults*: changing an add-on option restarts the add-on, which
interrupts every download in flight, so each knob worth changing day to day
is also editable in the app's Settings panel and persisted in the add-on's
own /data. Runtime values win while they are set.

The one option that needs real work is `zim_path`: ZIM archives live on a
network share mapped into Home Assistant as Media storage, and a share can be
unmounted, renamed or read-only at any moment. Nothing here raises on a
broken share - `storage_status()` reports what is wrong and the rest of the
app degrades to a clear message instead of crashing.
"""

import json
import os
import shutil
import threading
from datetime import datetime, time as clock_time
from pathlib import Path

# Where Home Assistant mounts Media-usage network storage. Overridable so the
# tests can point the whole app at a temporary directory.
MEDIA_ROOT = Path(os.environ.get("MEDIA_ROOT", "/media"))

# The add-on's own persistent storage - state only, never ZIM files.
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))

LIBRARY_SOURCE = os.environ.get("LIBRARY_SOURCE", "https://library.kiwix.org").rstrip("/")

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


# ── Runtime settings ────────────────────────────────────────────────────────

def _env_int(name: str, fallback: int) -> int:
    try:
        return int(os.environ.get(name, "") or fallback)
    except ValueError:
        return fallback


def _env_bool(name: str, fallback: bool) -> bool:
    raw = os.environ.get(name, "")
    return fallback if raw == "" else raw.strip().lower() == "true"


# Inclusive (min, max) bounds, mirroring config.yaml's schema so the in-app
# panel can never store a value the add-on options UI would itself reject.
_BOUNDS = {
    "max_concurrent_downloads": (1, 6),
    "download_threads": (1, 8),
}

_STR_KEYS = ("catalog_language", "window_start", "window_end")
_BOOL_KEYS = ("auto_serve_new", "window_enabled")

_lock = threading.RLock()
_overrides: dict | None = None


def defaults() -> dict:
    return {
        "max_concurrent_downloads": _env_int("MAX_CONCURRENT_DOWNLOADS", 1),
        "download_threads": _env_int("DOWNLOAD_THREADS", 1),
        "auto_serve_new": _env_bool("AUTO_SERVE_NEW", True),
        "catalog_language": (os.environ.get("CATALOG_LANGUAGE", "eng") or "eng").strip(),
        "window_enabled": _env_bool("DOWNLOAD_WINDOW_ENABLED", False),
        "window_start": (os.environ.get("DOWNLOAD_WINDOW_START", "23:00") or "23:00").strip(),
        "window_end": (os.environ.get("DOWNLOAD_WINDOW_END", "07:00") or "07:00").strip(),
    }


def _settings_file() -> Path:
    return DATA_DIR / "settings.json"


def _load() -> dict:
    global _overrides
    with _lock:
        if _overrides is None:
            try:
                raw = json.loads(_settings_file().read_text())
                _overrides = raw if isinstance(raw, dict) else {}
            except (OSError, ValueError):
                _overrides = {}
        return dict(_overrides)


def _clamp(key: str, value: int) -> int:
    low, high = _BOUNDS[key]
    return max(low, min(high, value))


def _parse_clock(value: str) -> clock_time | None:
    """'23:00' as a time, or None if it isn't a valid 24-hour clock time."""
    parts = str(value).strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return clock_time(hour, minute)


def all_settings() -> dict:
    values = defaults()
    for key, value in _load().items():
        if key in values:
            values[key] = value
    for key in _BOUNDS:
        values[key] = _clamp(key, int(values[key]))
    return values


def get(key: str):
    return all_settings()[key]


def set_many(values: dict) -> dict:
    """Persist the given settings, ignoring unknown keys and invalid values,
    clamping integers into range. Returns the resulting full settings."""
    global _overrides
    stored = _load()
    for key, value in values.items():
        if key in _BOUNDS:
            try:
                stored[key] = _clamp(key, int(value))
            except (TypeError, ValueError):
                continue
        elif key in _BOOL_KEYS:
            stored[key] = bool(value)
        elif key in _STR_KEYS:
            text = str(value).strip()
            if key in ("window_start", "window_end") and _parse_clock(text) is None:
                continue
            stored[key] = text

    with _lock:
        _overrides = stored
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _settings_file().with_suffix(".json.tmp")
        tmp.write_text(json.dumps(stored, indent=2))
        tmp.replace(_settings_file())

    return all_settings()


def reset_cache() -> None:
    """Drop the in-memory copy so the next read re-reads the file (tests)."""
    global _overrides
    with _lock:
        _overrides = None


# ── Download window ─────────────────────────────────────────────────────────

def window_state(now: datetime | None = None) -> dict:
    """Whether downloads may run right now, and when that next changes.

    Times are the container's local time, which the Supervisor sets from Home
    Assistant's own timezone. A window whose start and end are equal is
    treated as always open rather than as a zero-length window that would
    stall every download forever.
    """
    values = all_settings()
    now = now or datetime.now()
    start = _parse_clock(values["window_start"])
    end = _parse_clock(values["window_end"])

    if not values["window_enabled"] or start is None or end is None or start == end:
        return {"enabled": bool(values["window_enabled"]), "open": True, "start": values["window_start"], "end": values["window_end"]}

    current = now.time()
    # A window that wraps past midnight (23:00 -> 07:00) is open when the
    # time is after the start *or* before the end; a same-day one needs both.
    if start < end:
        is_open = start <= current < end
    else:
        is_open = current >= start or current < end

    return {
        "enabled": True,
        "open": is_open,
        "start": values["window_start"],
        "end": values["window_end"],
    }


def window_open(now: datetime | None = None) -> bool:
    return window_state(now)["open"]


# ── Storage ─────────────────────────────────────────────────────────────────

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
    """The add-on options as configured, for display in the UI."""
    return {"zim_path": raw_zim_path(), "library_source": LIBRARY_SOURCE, **defaults()}
