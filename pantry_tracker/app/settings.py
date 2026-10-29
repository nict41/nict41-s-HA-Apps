"""App settings.

Operational knobs (notify_days_before, notify_repeat_hours,
check_interval_hours, notify_service) are runtime-adjustable: each one's
default comes from the add-on options, but the in-app settings page can
override any of them, persisted in the `app_state` table under a
`setting:` prefix and read live on every check cycle - same pattern as
Parcel Tracker's settings.py.

The vision model connection (vision_api_base/vision_api_key/vision_model)
stays add-on-configuration-only, the same reasoning Parcel Tracker applies
to mailbox credentials and tracking-provider API keys: it's a secret-ish
connection detail, not a day-to-day tunable, and belongs in the Supervisor's
managed config rather than this app's own database.
"""

import os

import db

_PREFIX = "setting:"

_DEFAULTS = {
    "notify_days_before": int(os.environ.get("NOTIFY_DAYS_BEFORE", "3")),
    "notify_repeat_hours": int(os.environ.get("NOTIFY_REPEAT_HOURS", "24")),
    "check_interval_hours": int(os.environ.get("CHECK_INTERVAL_HOURS", "12")),
    "notify_service": os.environ.get("NOTIFY_SERVICE", ""),
}

# Inclusive (min, max) bounds, mirroring config.yaml's schema so the in-app
# page can never store a value the add-on options UI would itself reject.
_BOUNDS = {
    "notify_days_before": (0, 30),
    "notify_repeat_hours": (1, 168),
    "check_interval_hours": (1, 24),
}

INT_KEYS = tuple(_BOUNDS)
STR_KEYS = ("notify_service",)


def _clamp(key: str, value: int) -> int:
    lo, hi = _BOUNDS[key]
    return max(lo, min(hi, value))


def get_int(key: str) -> int:
    raw = db.get_state(_PREFIX + key)
    if raw is None:
        return _DEFAULTS[key]
    try:
        return _clamp(key, int(raw))
    except (TypeError, ValueError):
        return _DEFAULTS[key]


def get_str(key: str) -> str:
    raw = db.get_state(_PREFIX + key)
    return _DEFAULTS[key] if raw is None else raw


def all_settings() -> dict:
    values = {key: get_int(key) for key in INT_KEYS}
    values.update({key: get_str(key) for key in STR_KEYS})
    return values


def set_many(values: dict) -> dict:
    """Persist the given settings (ignoring unknown keys / invalid numbers),
    clamping integers into range. Returns the resulting full settings dict."""
    for key, value in values.items():
        if key in _BOUNDS:
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            db.set_state(_PREFIX + key, str(_clamp(key, number)))
        elif key in STR_KEYS:
            db.set_state(_PREFIX + key, str(value).strip())
    return all_settings()


# ── Vision model connection (add-on configuration only) ─────────────────────


def vision_api_base() -> str:
    return os.environ.get("VISION_API_BASE", "").strip().rstrip("/")


def vision_api_key() -> str:
    return os.environ.get("VISION_API_KEY", "").strip()


def vision_model() -> str:
    return os.environ.get("VISION_MODEL", "").strip() or "qwen2.5vl:3b"
