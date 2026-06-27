"""Runtime-adjustable settings, layered over the add-on configuration.

Each setting's default comes from the add-on options (exported as env vars by
the run script). The in-app settings page can override any of these at
runtime; overrides are persisted in the app_state table under a `setting:`
prefix and read live on every sync, so a change takes effect on the next
cycle without an add-on restart.

Only non-secret operational knobs live here. Mailbox credentials and tracking
provider API keys stay in the add-on configuration (the managed, secret
store) on purpose - see the project docs for the rationale.
"""

import os

import db

_PREFIX = "setting:"

# Defaults sourced from the add-on options. Read once at import - they're the
# fallback when a setting hasn't been overridden in-app, not the live value.
_DEFAULTS = {
    "poll_interval_minutes": int(os.environ.get("POLL_INTERVAL_MINUTES", "30")),
    "provider_refresh_minutes": int(os.environ.get("PROVIDER_REFRESH_MINUTES", "0")),
    "lookback_days": int(os.environ.get("LOOKBACK_DAYS", "14")),
    "auto_archive_after_days": int(os.environ.get("AUTO_ARCHIVE_AFTER_DAYS", "14")),
    "dismiss_unconfirmed_after_days": int(os.environ.get("DISMISS_UNCONFIRMED_AFTER_DAYS", "3")),
    "trusted_senders": os.environ.get("TRUSTED_SENDERS", ""),
    "ignore_senders": os.environ.get("IGNORE_SENDERS", ""),
    "allowed_senders": os.environ.get("ALLOWED_SENDERS", ""),
}

# Inclusive (min, max) bounds for the integer settings, mirroring config.yaml's
# schema so the in-app page can never store a value the add-on options UI would
# itself reject. provider_refresh_minutes is in-app only (0 = no throttle).
_BOUNDS = {
    "poll_interval_minutes": (5, 1440),
    "provider_refresh_minutes": (0, 10080),
    "lookback_days": (1, 90),
    "auto_archive_after_days": (0, 365),
    "dismiss_unconfirmed_after_days": (0, 90),
}

INT_KEYS = tuple(_BOUNDS)
STR_KEYS = ("trusted_senders", "ignore_senders", "allowed_senders")


def _clamp(key: int, value: int) -> int:
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


def get_domains(key: str) -> frozenset[str]:
    """A comma-separated sender-domain setting as a normalised set."""
    return frozenset(d.strip().lower() for d in get_str(key).split(",") if d.strip())


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
