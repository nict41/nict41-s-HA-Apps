"""Loading and validating /config/screens.yaml.

A bad screen is reported (returned in `errors`) and skipped; the rest of the
file still works. Validation is structural only - template strings are
rendered later, at render time.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import yaml

from night import NightConfig, parse_night

_LOG = logging.getLogger(__name__)

CLOCK_ENTRY = "clock"  # rotation entry that hands the tubes back to the clock
WIDGET_TYPES = ("text", "big_number", "ring", "bar", "icon", "image", "sparkline", "custom", "blank")
ITEM_TYPES = ("text", "ring", "bar", "icon", "image", "rect")   # what a custom widget is made of
FITS = ("cover", "contain")


class ScreensError(Exception):
    """The whole screens file is unusable (not a mapping, bad YAML, ...)."""


@dataclass
class Screen:
    name: str
    duration: int | None = None       # None -> default_duration
    refresh: int | None = None        # None -> default_refresh
    when: str | None = None           # HA template; skipped in rotation unless "true"
    background: str = "#000000"
    tubes: list[dict] = field(default_factory=list)   # exactly 6 widgets
    strip: dict | None = None         # alternative: one 810x240 picture

    def resolved_duration(self, default: int) -> int:
        return self.duration if self.duration is not None else default

    def resolved_refresh(self, default: int) -> int:
        return self.refresh if self.refresh is not None else default


@dataclass
class ClockOverride:
    """Per-clock override of the global rotation / clock duration (screens.yaml `clocks:`)."""

    rotation: list[str] | None = None
    clock_duration: int | None = None


@dataclass
class ScreensConfig:
    rotation: list[str] = field(default_factory=list)
    clock_duration: int | None = None   # duration for the "clock" rotation entry
    screens: dict[str, Screen] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)  # human-readable, per screen
    clock_overrides: dict[str, ClockOverride] = field(default_factory=dict)  # clock name -> override
    night: NightConfig | None = None     # the `night:` block, None when absent

    @property
    def ok(self) -> bool:
        return not self.errors

    def rotation_for(self, clock_name: str) -> list[str]:
        """The rotation for a clock: its override if it has one, else the global one."""
        ov = self.clock_overrides.get(clock_name)
        if ov is not None and ov.rotation is not None:
            return list(ov.rotation)
        return list(self.rotation)

    def clock_duration_for(self, clock_name: str) -> int | None:
        """The "clock" entry duration for a clock: its override if set, else the global one."""
        ov = self.clock_overrides.get(clock_name)
        if ov is not None and ov.clock_duration is not None:
            return ov.clock_duration
        return self.clock_duration


def _err(errors: list[str], screen: str, msg: str) -> None:
    errors.append(f"screen '{screen}': {msg}")


def _check_widget(errors: list[str], screen: str, idx: int, w: object) -> bool:
    where = f"screen '{screen}' tube {idx}"
    if not isinstance(w, dict):
        errors.append(f"{where}: widget must be a mapping, got {type(w).__name__}")
        return False
    wtype = w.get("type")
    if wtype not in WIDGET_TYPES:
        errors.append(f"{where}: unknown widget type {wtype!r} (expected one of {', '.join(WIDGET_TYPES)})")
        return False
    if wtype == "image":
        src = w.get("source")
        if not isinstance(src, str) or not src:
            errors.append(f"{where}: image widget needs a 'source' (builtin:..., file:<uploaded picture>, http(s)://..., camera:..., entity_picture:...)")
            return False
        fit = w.get("fit", "cover")
        if fit not in FITS:
            errors.append(f"{where}: image 'fit' must be one of {', '.join(FITS)}, got {fit!r}")
            return False
    if wtype == "sparkline":
        if not isinstance(w.get("entity"), str) or not w["entity"]:
            errors.append(f"{where}: sparkline widget needs an 'entity'")
            return False
    if wtype == "icon":
        icon = w.get("icon")
        if not isinstance(icon, str) or not icon.startswith("mdi:"):
            errors.append(f"{where}: icon widget needs 'icon: mdi:<name>'")
            return False
    if wtype in ("ring", "bar", "big_number"):
        if "value" not in w:
            errors.append(f"{where}: {wtype} widget needs a 'value'")
            return False
    if wtype == "custom":
        items = w.get("items", [])
        if not isinstance(items, list):
            errors.append(f"{where}: custom widget 'items' must be a list")
            return False
        for n, it in enumerate(items):
            if not isinstance(it, dict) or it.get("type") not in ITEM_TYPES:
                kind = it.get("type") if isinstance(it, dict) else type(it).__name__
                errors.append(f"{where} item {n}: unknown item type {kind!r} (expected one of {', '.join(ITEM_TYPES)})")
                return False
            if it["type"] == "image" and it.get("fit", "contain") not in FITS:
                errors.append(f"{where} item {n}: image 'fit' must be one of {', '.join(FITS)}")
                return False
    return True


def _parse_screen(name: str, raw: object, errors: list[str]) -> Screen | None:
    if not isinstance(raw, dict):
        _err(errors, name, "must be a mapping")
        return None
    screen = Screen(name=name)
    for key in ("duration", "refresh"):
        val = raw.get(key)
        if val is not None:
            if isinstance(val, bool) or not isinstance(val, (int, float)) or val <= 0:
                _err(errors, name, f"'{key}' must be a positive number, got {val!r}")
                return None
            setattr(screen, key, int(val))
    when = raw.get("when")
    if when is not None and not isinstance(when, str):
        _err(errors, name, f"'when' must be a template string, got {type(when).__name__}")
        return None
    screen.when = when
    bg = raw.get("background", "#000000")
    if not isinstance(bg, str):
        _err(errors, name, f"'background' must be a string, got {type(bg).__name__}")
        return None
    screen.background = bg

    has_tubes = "tubes" in raw
    has_strip = "strip" in raw
    if has_tubes and has_strip:
        _err(errors, name, "use either 'tubes' or 'strip', not both")
        return None
    if not has_tubes and not has_strip:
        _err(errors, name, "needs 'tubes' (6 widgets) or 'strip' (one 810x240 picture)")
        return None
    if has_strip:
        strip = raw["strip"]
        if not isinstance(strip, dict) or not isinstance(strip.get("source"), str) or not strip["source"]:
            _err(errors, name, "'strip' must be a mapping with a 'source' (camera:..., http(s)://..., ...)")
            return None
        fit = strip.get("fit", "cover")
        if fit not in FITS:
            _err(errors, name, f"strip 'fit' must be one of {', '.join(FITS)}, got {fit!r}")
            return None
        screen.strip = strip
    else:
        tubes = raw["tubes"]
        if not isinstance(tubes, list) or len(tubes) != 6:
            n = len(tubes) if isinstance(tubes, list) else type(tubes).__name__
            _err(errors, name, f"'tubes' must be a list of exactly 6 widgets, got {n}")
            return None
        for i, w in enumerate(tubes):
            if not _check_widget(errors, name, i, w):
                return None
        screen.tubes = list(tubes)
    return screen


def _parse_clock_overrides(data: dict, cfg: ScreensConfig) -> None:
    """The optional top-level `clocks:` mapping: clock name -> {rotation, clock}.

    Validated like the global ones; problems go to cfg.errors (never raised) and
    only the bad key is skipped."""
    raw = data.get("clocks")
    if raw is None:
        return
    if not isinstance(raw, dict):
        cfg.errors.append(f"'clocks' must be a mapping of clock name -> {{rotation, clock}}, got {type(raw).__name__}")
        return
    for name, entry in raw.items():
        if not isinstance(name, str) or not name:
            cfg.errors.append(f"'clocks' key must be a non-empty clock name, got {name!r}")
            continue
        if entry is None:
            entry = {}
        if not isinstance(entry, dict):
            cfg.errors.append(f"clocks '{name}': must be a mapping, got {type(entry).__name__}")
            continue
        ov = ClockOverride()
        rotation = entry.get("rotation")
        if rotation is not None:
            if not isinstance(rotation, list) or not all(isinstance(r, str) for r in rotation) or not rotation:
                cfg.errors.append(f"clocks '{name}': 'rotation' must be a non-empty list of screen names")
            else:
                ov.rotation = list(rotation)
        clock = entry.get("clock")
        if clock is not None:
            if not isinstance(clock, dict):
                cfg.errors.append(f"clocks '{name}': 'clock' must be a mapping, e.g. {{duration: 60}}")
            else:
                dur = clock.get("duration")
                if dur is not None and (isinstance(dur, bool) or not isinstance(dur, (int, float)) or dur <= 0):
                    cfg.errors.append(f"clocks '{name}': 'clock.duration' must be a positive number, got {dur!r}")
                else:
                    ov.clock_duration = int(dur) if dur is not None else None
        cfg.clock_overrides[name] = ov


def parse_screens(data: object) -> ScreensConfig:
    """Parse an already-YAML-loaded object into a ScreensConfig."""
    cfg = ScreensConfig()
    if not isinstance(data, dict):
        raise ScreensError(f"screens file must be a mapping, got {type(data).__name__}")

    rotation = data.get("rotation", [])
    if not isinstance(rotation, list) or not all(isinstance(r, str) for r in rotation):
        raise ScreensError("'rotation' must be a list of screen names")
    if not rotation:
        raise ScreensError("'rotation' must list at least one screen")
    cfg.rotation = list(rotation)

    clock = data.get("clock")
    if clock is not None:
        if not isinstance(clock, dict):
            raise ScreensError("'clock' must be a mapping, e.g. {duration: 60}")
        dur = clock.get("duration")
        if dur is not None and (isinstance(dur, bool) or not isinstance(dur, (int, float)) or dur <= 0):
            raise ScreensError(f"'clock.duration' must be a positive number, got {dur!r}")
        cfg.clock_duration = int(dur) if dur is not None else None

    _parse_clock_overrides(data, cfg)

    night_cfg, night_errors = parse_night(data.get("night"))
    cfg.night = night_cfg
    for e in night_errors:
        cfg.errors.append(e)

    screens_raw = data.get("screens")
    if not isinstance(screens_raw, dict) or not screens_raw:
        raise ScreensError("'screens' must be a non-empty mapping of screen name -> definition")

    for name, raw in screens_raw.items():
        if not isinstance(name, str) or not name:
            cfg.errors.append(f"screen name must be a non-empty string, got {name!r}")
            continue
        screen = _parse_screen(name, raw, cfg.errors)
        if screen is not None:
            cfg.screens[name] = screen

    for entry in cfg.rotation:
        if entry != CLOCK_ENTRY and entry not in cfg.screens:
            cfg.errors.append(f"rotation entry '{entry}' has no matching screen in 'screens'")
    # per-clock rotations reference screens too
    for name, ov in cfg.clock_overrides.items():
        for entry in (ov.rotation or []):
            if entry != CLOCK_ENTRY and entry not in cfg.screens:
                cfg.errors.append(f"clocks '{name}': rotation entry '{entry}' has no matching screen in 'screens'")
    return cfg


def load_screens(path: str) -> ScreensConfig:
    """Load and validate the screens file. Raises ScreensError if unusable."""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if data is None:
        raise ScreensError("screens file is empty")
    return parse_screens(data)


def example_screens_yaml() -> str:
    """The commented example written to /config/screens.yaml on first start."""
    return """\
# Tube Display screens. Re-read automatically when this file changes.
# Easiest to edit with the Tube Display panel in the Home Assistant sidebar.
#
# rotation lists the screens to cycle through, in order. The special entry
# "clock" hands the tubes back to the clock's own time display for the
# duration given under `clock:`.
rotation: [claude, clock]
clock: {duration: 60}

screens:
  # A screen is either `tubes:` (exactly 6 widgets, left to right, one per
  # 135x240 tube) or `strip:` (one 810x240 picture across all six tubes).
  claude:
    duration: 20        # optional, default_duration from the add-on options
    refresh: 30         # optional, default_refresh; seconds between re-renders
    # when: "{{ is_state('binary_sensor.doorbell','on') }}"  # optional; the
    #   screen is skipped in the rotation unless the template renders "true"
    background: "#000000"
    tubes:
      - {type: image, source: "builtin:claude"}
      - {type: ring, value: "{{ states('sensor.example_session_usage') }}", max: 100,
         label: "Session", unit: "%", color: "#D97757"}
      - {type: ring, value: "{{ states('sensor.example_week_usage') }}", max: 100,
         label: "Week", unit: "%", color: "#D97757"}
      - {type: text, text: "Resets\\n{{ states('sensor.example_resets') }}",
         size: auto, color: "#FFFFFF", align: center}
      - {type: big_number, value: "{{ states('sensor.example_pace') }}", unit: "%", label: "Pace"}
      - {type: icon, icon: "mdi:robot", text: "{{ states('sensor.example_status') }}", color: "#D97757"}

  # One picture across all six tubes (fit: cover crops to fill 810x240).
  doorbell:
    strip: {source: "camera:camera.front_door", fit: cover}

# Widget types (every string field may be a Home Assistant template, and text fields
# may have several lines with \\n):
#   text       text, size: auto fits the tube (or a number), align: left|center|right,
#              valign: top|middle|bottom, weight: bold, color
#   big_number value + unit + label, auto-sized to fit (value_size, unit_size, label_size)
#   ring       circular gauge 0..max, value in the middle, label below (several lines move
#              the ring up), color, thickness, radius, value_size, unit_size, label_size
#   bar        vertical bar gauge 0..max, value + label
#   icon       mdi:<name> icon with optional text below, color, icon_size, text_size
#   image      source: builtin:claude | file:<uploaded picture> | http(s)://... | camera:<entity_id> |
#              entity_picture:<entity_id>, fit: cover|contain, zoom, offset_x, offset_y, rotate,
#              background (the editor sets these when you drag the picture into place)
#   sparkline  entity + hours, line graph from the HA history API, latest value shown
#   custom     items: a list of text / ring / bar / icon / image / rect, each placed by x, y
#              (easiest with the editor's designer)
#   blank      empty tube
# Colours accept "#RRGGBB" or a template returning one.
"""
