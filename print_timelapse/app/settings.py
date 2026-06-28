"""Runtime-adjustable settings, layered over the add-on configuration.

Each setting's default comes from the add-on options (exported as env vars by
the run script). The in-app settings page can override any of them; overrides
are persisted to a small JSON file in DATA_DIR and read live wherever the
value is used - which, for this add-on, is only when a GIF is built on
`/finish`. So a change takes effect on the next finished timelapse with no
add-on restart, and no database is needed for four values.
"""

import json
import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
_SETTINGS_PATH = DATA_DIR / "settings.json"


def _env_bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() == "true"


# Defaults sourced from the add-on options. Read once at import - they're the
# fallback when a setting hasn't been overridden in-app, not the live value.
_DEFAULTS = {
    "gif_fps": int(os.environ.get("GIF_FPS", "8")),
    "gif_width": int(os.environ.get("GIF_WIDTH", "480")),
    "cleanup_after_finish": _env_bool("CLEANUP_AFTER_FINISH", True),
    "gif_export_path": os.environ.get("GIF_EXPORT_PATH", ""),
    # Entity IDs the in-app Help page's "create automations" customizer
    # collects, persisted so the form survives a reload while filling it in.
    "print_status_entity": "",
    "print_progress_entity": "",
    "snapshot_camera_entity": "",
    "snapshot_image_url": "",
}

# Inclusive (min, max) bounds for integer settings, mirroring config.yaml's
# schema so the in-app page can't store a value the add-on options would reject.
_BOUNDS = {
    "gif_fps": (1, 30),
    "gif_width": (160, 1920),
}

INT_KEYS = ("gif_fps", "gif_width")
BOOL_KEYS = ("cleanup_after_finish",)
STR_KEYS = (
    "gif_export_path",
    "print_status_entity",
    "print_progress_entity",
    "snapshot_camera_entity",
    "snapshot_image_url",
)


def _load() -> dict:
    try:
        with open(_SETTINGS_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _clamp(key: str, value: int) -> int:
    lo, hi = _BOUNDS[key]
    return max(lo, min(hi, value))


def _sanitize_path(value) -> str:
    """A media-relative export path - leading/trailing slashes trimmed and any
    `.`/`..`/empty segments dropped, so it can never escape the /media mount."""
    parts = [p for p in str(value).strip().strip("/").split("/") if p not in ("", ".", "..")]
    return "/".join(parts)


def get_int(key: str) -> int:
    raw = _load().get(key)
    if raw is None:
        return _clamp(key, _DEFAULTS[key])
    try:
        return _clamp(key, int(raw))
    except (TypeError, ValueError):
        return _clamp(key, _DEFAULTS[key])


def get_bool(key: str) -> bool:
    raw = _load().get(key)
    return raw if isinstance(raw, bool) else _DEFAULTS[key]


def get_str(key: str) -> str:
    raw = _load().get(key)
    value = _DEFAULTS[key] if raw is None else raw
    return _sanitize_path(value) if key == "gif_export_path" else str(value)


def all_settings() -> dict:
    values = {key: get_int(key) for key in INT_KEYS}
    values.update({key: get_bool(key) for key in BOOL_KEYS})
    values.update({key: get_str(key) for key in STR_KEYS})
    return values


def _atomic_write(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _SETTINGS_PATH.parent / (_SETTINGS_PATH.name + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, _SETTINGS_PATH)


def set_many(values: dict) -> dict:
    """Persist the given settings (ignoring unknown keys / invalid numbers),
    clamping integers into range. Returns the resulting full settings dict."""
    current = _load()
    for key, value in values.items():
        if key in _BOUNDS:
            try:
                current[key] = _clamp(key, int(value))
            except (TypeError, ValueError):
                continue
        elif key in BOOL_KEYS:
            current[key] = bool(value)
        elif key in STR_KEYS:
            current[key] = _sanitize_path(value) if key == "gif_export_path" else str(value).strip()
    _atomic_write(current)
    return all_settings()
