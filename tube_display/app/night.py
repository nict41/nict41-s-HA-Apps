"""Night mode: pure maths for daylight, brightness, dimming and quiet hours.

Everything here is a pure function (no Pillow, no I/O) so it is fully testable
without the rendering stack. screens.py parses the `night:` block of
screens.yaml into a NightConfig via parse_night(); the controller calls the
functions here about once a minute.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass

MIN_BRIGHTNESS = 10
MAX_BRIGHTNESS = 255

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


@dataclass
class QuietHours:
    """Local-time quiet hours; may span midnight. `mode` is "clock" or "blank"."""

    start: _dt.time
    end: _dt.time
    mode: str = "clock"   # clock = hand the tubes back to the clock, blank = black pictures


@dataclass
class NightConfig:
    """The `night:` block of screens.yaml (all keys optional at parse time)."""

    brightness_day: int = MAX_BRIGHTNESS
    brightness_night: int = MIN_BRIGHTNESS
    bright_above: float = 3.0     # sun elevation at/above which it is full day
    dim_below: float = -6.0       # at/below which it is full night
    dim_screens: float = 1.0      # colour multiplier at full night (1 = never dim)
    quiet: QuietHours | None = None


def daylight(elevation: float, bright_above: float, dim_below: float) -> float:
    """0..1 how much daylight there is: 1 at/above `bright_above`, 0 at/below
    `dim_below`, linear in between. Degenerate ranges (bright_above <= dim_below)
    give a hard step at bright_above."""
    if bright_above <= dim_below:
        return 1.0 if elevation >= bright_above else 0.0
    if elevation >= bright_above:
        return 1.0
    if elevation <= dim_below:
        return 0.0
    return (elevation - dim_below) / (bright_above - dim_below)


def brightness(daylight: float, day: int, night: int) -> int:
    """The clock's brightness for a daylight fraction, clamped to 10..255."""
    d = min(1.0, max(0.0, daylight))
    value = night + (day - night) * d
    return int(round(min(MAX_BRIGHTNESS, max(MIN_BRIGHTNESS, value))))


def dim_factor(daylight: float, dim_screens: float) -> float:
    """Multiply the screens' colours by this: 1 in full daylight, `dim_screens`
    at full night, linear in between (clamped to 0..1)."""
    d = min(1.0, max(0.0, daylight))
    f = min(1.0, max(0.0, dim_screens))
    return 1.0 + (f - 1.0) * (1.0 - d)


def in_quiet(now: _dt.time, start: _dt.time, end: _dt.time) -> bool:
    """True when `now` is inside the quiet window [start, end). A window that
    spans midnight (start > end) wraps; start == end means never quiet."""
    if start == end:
        return False
    if start < end:
        return start <= now < end
    return now >= start or now < end


def _parse_time(raw: object, errors: list[str], where: str) -> _dt.time | None:
    if not isinstance(raw, str):
        errors.append(f"{where}: must be a 'HH:MM' string, got {raw!r}")
        return None
    m = _TIME_RE.match(raw.strip())
    if not m:
        errors.append(f"{where}: must be a 'HH:MM' string, got {raw!r}")
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        errors.append(f"{where}: not a valid time of day, got {raw!r}")
        return None
    return _dt.time(h, mi)


def _num(raw: object, errors: list[str], where: str, lo: float, hi: float) -> float | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        errors.append(f"{where}: must be a number, got {raw!r}")
        return None
    v = float(raw)
    if v < lo or v > hi:
        errors.append(f"{where}: must be between {lo:g} and {hi:g}, got {raw!r}")
        return None
    return v


def parse_night(raw: object) -> tuple[NightConfig | None, list[str]]:
    """Parse the `night:` block of screens.yaml -> (config or None, errors).

    An absent block (None) means the app never touches brightness. A present but
    empty block means defaults. Errors are reported, not raised, and the block is
    then ignored (None) so a typo cannot break the rotation."""
    if raw is None:
        return None, []
    errors: list[str] = []
    if not isinstance(raw, dict):
        return None, [f"'night' must be a mapping, got {type(raw).__name__}"]
    cfg = NightConfig()
    day = _num(raw.get("brightness_day", MAX_BRIGHTNESS), errors, "night.brightness_day",
               MIN_BRIGHTNESS, MAX_BRIGHTNESS)
    if day is not None:
        cfg.brightness_day = int(day)
    night_b = _num(raw.get("brightness_night", MIN_BRIGHTNESS), errors, "night.brightness_night",
                   MIN_BRIGHTNESS, MAX_BRIGHTNESS)
    if night_b is not None:
        cfg.brightness_night = int(night_b)
    above = _num(raw.get("bright_above", 3.0), errors, "night.bright_above", -90.0, 90.0)
    if above is not None:
        cfg.bright_above = above
    below = _num(raw.get("dim_below", -6.0), errors, "night.dim_below", -90.0, 90.0)
    if below is not None:
        cfg.dim_below = below
    dim = _num(raw.get("dim_screens", 1.0), errors, "night.dim_screens", 0.0, 1.0)
    if dim is not None:
        cfg.dim_screens = dim
    quiet_raw = raw.get("quiet")
    if quiet_raw is not None:
        if not isinstance(quiet_raw, dict):
            errors.append(f"night.quiet must be a mapping, got {type(quiet_raw).__name__}")
        else:
            start = _parse_time(quiet_raw.get("from"), errors, "night.quiet.from")
            end = _parse_time(quiet_raw.get("to"), errors, "night.quiet.to")
            mode = str(quiet_raw.get("mode", "clock")).strip().lower()
            if mode not in ("clock", "blank"):
                errors.append(f"night.quiet.mode must be 'clock' or 'blank', got {mode!r}")
                mode = "clock"
            if start is not None and end is not None:
                cfg.quiet = QuietHours(start=start, end=end, mode=mode)
    if errors:
        return None, errors
    return cfg, []
