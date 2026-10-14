"""App options, read from the environment the run script exports.

There is nothing persisted and nothing editable at runtime: this app has one
job, five knobs, and every one of them only matters at startup, so the HA
options UI is the only place they live. Values are read lazily through
functions rather than captured into module constants, so the tests can
monkeypatch the environment without re-importing the whole app.

`camera_entity` is validated against the same pattern config.yaml enforces.
That is not belt-and-braces for the options UI - the entity id is
interpolated into a Home Assistant API path, so anything that isn't
`camera.<lowercase_id>` is rejected here before it can become a request for
some other endpoint.
"""

import hashlib
import hmac
import os
import re

# Home Assistant Core API, reached through the Supervisor's internal proxy.
# Deliberately *not* called HA_URL: that name is already used by other
# tooling on this stack and would collide. Override only for development.
HA_API_DEFAULT = "http://supervisor/core/api"

CAMERA_ENTITY_DEFAULT = "camera.centauri_carbon_2_chamber_camera"
PAGE_TITLE_DEFAULT = "3D Printer Cam"
FPS_DEFAULT = 5
MAX_VIEWERS_DEFAULT = 5

# Mirrors config.yaml's `match(^camera\.[a-z0-9_]+$)`.
ENTITY_RE = re.compile(r"^camera\.[a-z0-9_]+$")

# Inclusive bounds, mirroring config.yaml's schema.
FPS_BOUNDS = (1, 15)
MAX_VIEWERS_BOUNDS = (1, 20)

# How the fan-out behaves. These are not options: they are properties of the
# printer camera this app exists to protect, and getting them wrong is how
# you crash its firmware.
#
# IDLE_SHUTDOWN_SECONDS - how long the single upstream subscription is held
#   open after the last viewer leaves. Long enough that a page reload or a
#   viewer switching networks doesn't tear the subscription down and build
#   it back up, short enough that the integration's own frame hub (which
#   stops 60 s after its last consumer) is released promptly.
# STALL_SECONDS - a camera_proxy_stream connection stays open and simply
#   goes quiet when the printer's camera server dies, so silence is the only
#   symptom there is. After this long with no frame, the subscription is
#   closed and retried rather than held open forever.
# BACKOFF_* - reconnect delay, doubling with jitter. Never a tight loop: a
#   tight retry loop against a camera that allows exactly one connection is
#   what takes the firmware down.
IDLE_SHUTDOWN_SECONDS = 15.0
STALL_SECONDS = 10.0
BACKOFF_START_SECONDS = 2.0
BACKOFF_MAX_SECONDS = 60.0
BACKOFF_JITTER = 0.25

# A frame older than this is not served as a snapshot. Matches the staleness
# window the HA integration itself applies to /api/camera_proxy.
SNAPSHOT_MAX_AGE_SECONDS = 5.0


def _env_int(name: str, fallback: int, bounds: tuple[int, int]) -> int:
    low, high = bounds
    try:
        value = int(os.environ.get(name, "") or fallback)
    except ValueError:
        value = fallback
    return max(low, min(high, value))


def ha_api() -> str:
    return (os.environ.get("HA_API", "") or HA_API_DEFAULT).rstrip("/")


def supervisor_token() -> str:
    return os.environ.get("SUPERVISOR_TOKEN", "").strip()


def camera_entity() -> str:
    """The configured camera, or the default if it is missing or malformed.

    Falling back rather than raising is deliberate: a typo in the entity id
    should leave the app up and reporting "camera offline" on a page the
    owner can read, not leave it crash-looping with the reason buried in
    the Supervisor log.
    """
    value = (os.environ.get("CAMERA_ENTITY", "") or "").strip()
    return value if ENTITY_RE.match(value) else CAMERA_ENTITY_DEFAULT


def page_title() -> str:
    return (os.environ.get("PAGE_TITLE", "") or PAGE_TITLE_DEFAULT).strip() or PAGE_TITLE_DEFAULT


def fps() -> int:
    return _env_int("FPS", FPS_DEFAULT, FPS_BOUNDS)


def max_viewers() -> int:
    return _env_int("MAX_VIEWERS", MAX_VIEWERS_DEFAULT, MAX_VIEWERS_BOUNDS)


def access_key() -> str:
    """The shared key, or "" when the page is open to anyone who reaches it."""
    return os.environ.get("ACCESS_KEY", "") or ""


def auth_required() -> bool:
    return bool(access_key())


# ── Access key handling ─────────────────────────────────────────────────────

# The cookie holds a digest of the key, not the key: a browser's cookie jar,
# a crash dump or a shared device then never carries the key itself. It's an
# unsalted SHA-256 so that the cookie survives an app restart (a salt
# generated at startup would sign every viewer out on every update), which
# is an acceptable trade for a value that is already pasted into a URL and
# handed to friends.
COOKIE_NAME = "camshare"


def key_digest(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def key_matches(candidate: str | None) -> bool:
    """Whether a `?key=` value is the configured key, in constant time."""
    if not auth_required():
        return True
    if not candidate:
        return False
    return hmac.compare_digest(candidate.encode("utf-8"), access_key().encode("utf-8"))


def cookie_matches(candidate: str | None) -> bool:
    """Whether a cookie value is the digest of the configured key."""
    if not auth_required():
        return True
    if not candidate:
        return False
    return hmac.compare_digest(candidate, key_digest(access_key()))


def cookie_value() -> str:
    return key_digest(access_key())


def as_dict() -> dict:
    """The options as configured, for the startup log. Never the key itself."""
    return {
        "camera_entity": camera_entity(),
        "page_title": page_title(),
        "fps": fps(),
        "max_viewers": max_viewers(),
        "access_key_set": auth_required(),
        "ha_api": ha_api(),
    }
