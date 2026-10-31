"""App settings.

Numeric operational knobs (notify_days_before and notify_repeat_hours) are
runtime-adjustable: each one's default comes from the add-on options, but the in-app settings page can override any of
them, persisted in the `app_state` table under a `setting:` prefix and
read live on every check cycle - same pattern as Parcel Tracker's
settings.py.

notify_service, and the vision model connection
(vision_api_base/vision_api_key/vision_model), stay add-on-configuration-
only - read straight from the environment on every call, never persisted
to this app's own database. They were both runtime-overridable here too
at one point, but that let a value saved once (even blank, e.g. from
visiting the in-app Settings page before the add-on's own Configuration
had been set up yet) silently shadow the Configuration tab forever after,
which is exactly what it looks like when "the notification service keeps
getting lost after an update" - the add-on's Configuration still has the
right value the whole time, but this app kept reading a stale override
from its own database instead. A single source of truth that's also
exactly what Supervisor already persists correctly across upgrades avoids
that trap entirely.
"""

import os

import db

_PREFIX = "setting:"

_DEFAULTS = {
    "notify_days_before": int(os.environ.get("NOTIFY_DAYS_BEFORE", "3")),
    "notify_repeat_hours": int(os.environ.get("NOTIFY_REPEAT_HOURS", "24")),
    "notify_hour": int(os.environ.get("NOTIFY_HOUR", "8")),
}

# Inclusive (min, max) bounds, mirroring config.yaml's schema so the in-app
# page can never store a value the add-on options UI would itself reject.
_BOUNDS = {
    "notify_days_before": (0, 30),
    "notify_repeat_hours": (1, 168),
    "notify_hour": (0, 23),
}

INT_KEYS = tuple(_BOUNDS)


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


def all_settings() -> dict:
    values = {key: get_int(key) for key in INT_KEYS}
    values["notify_service"] = notify_service()
    return values


def set_many(values: dict) -> dict:
    """Persist the given numeric settings (ignoring unknown keys / invalid
    numbers), clamping into range. Returns the resulting full settings
    dict. notify_service isn't accepted here - see the module docstring."""
    for key, value in values.items():
        if key in _BOUNDS:
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            db.set_state(_PREFIX + key, str(_clamp(key, number)))
    return all_settings()


# ── Add-on configuration only (never persisted to this app's own database,
# so an upgrade or a restart can never leave a stale override behind -
# Supervisor already persists these correctly on its own) ──────────────────


def notify_service() -> str:
    return os.environ.get("NOTIFY_SERVICE", "").strip()


def vision_api_base() -> str:
    return os.environ.get("VISION_API_BASE", "").strip().rstrip("/")


def vision_api_key() -> str:
    return os.environ.get("VISION_API_KEY", "").strip()


def vision_model() -> str:
    return os.environ.get("VISION_MODEL", "").strip() or "glm-ocr:latest"
