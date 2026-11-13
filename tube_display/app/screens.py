"""Loading and validating /config/screens.yaml.

A bad screen is reported (returned in `errors`) and skipped; the rest of the
file still works. Validation is structural only - template strings are
rendered later, at render time.
"""

from __future__ import annotations

import difflib
import logging
from dataclasses import dataclass, field

import yaml

from night import NightConfig, parse_night

_LOG = logging.getLogger(__name__)

CLOCK_ENTRY = "clock"  # rotation entry that hands the tubes back to the clock
# Order matters: it is the order of the type <select> in the editor (graph sits next to
# sparkline, the rest before custom/blank)
WIDGET_TYPES = ("text", "big_number", "ring", "bar", "icon", "image", "sparkline", "graph",
                "progress", "analog_clock", "weather", "ticker", "gif", "custom", "blank")
ITEM_TYPES = ("text", "ring", "bar", "icon", "image", "rect")   # what a custom widget is made of
FITS = ("cover", "contain")
TRANSITIONS = ("none", "cascade", "cascade_right", "center_out", "wipe", "wipe_right", "wipe_center",
               "flip", "flip_right", "flip_center")
DEFAULT_TRANSITION = "none"
DEFAULT_TRANSITION_DELAY = 90   # ms between tubes during a transition
DEFAULT_TRANSITION_DURATION = 500   # ms a wipe takes from start to end
MAX_GIF_TUBES = 2               # the clock plays at most two GIFs at once


class ScreensError(Exception):
    """The whole screens file is unusable (not a mapping, bad YAML, ...)."""


# ---------------------------------------------------------------------------
# Known fields per mapping, for "did you mean" warnings on unknown keys.
# Built from what render.py actually reads (see its draw_* / _item_* functions);
# the editor's FIELDS / MORE / ITEM_FIELDS tables must all be covered (a test
# parses editor.js and checks this).
# ---------------------------------------------------------------------------

# fields every widget that draws text also takes
_TEXT_COMMON = ("font",)

KNOWN_FIELDS: dict[str, frozenset[str]] = {
    # screen level
    "screen": frozenset({"duration", "refresh", "when", "background", "transition",
                         "transition_delay", "transition_duration", "tubes", "strip", "canvas"}),
    # strip (one picture across all six tubes): the image placement fields
    "strip": frozenset({"source", "fit", "zoom", "offset_x", "offset_y", "rotate", "background"}),
    # canvas (free design across all six tubes)
    "canvas": frozenset({"background", "items"}),
    # widget types
    "text": frozenset({"type", "text", "size", "align", "valign", "weight", "color", *_TEXT_COMMON}),
    "big_number": frozenset({"type", "value", "unit", "label", "unit_newline", "color",
                             "value_size", "unit_size", "label_size", "value_color", "unit_color",
                             "label_color", *_TEXT_COMMON}),
    "ring": frozenset({"type", "value", "max", "label", "unit", "unit_newline", "color",
                       "radius", "thickness", "value_size", "unit_size", "label_size",
                       "value_color", "unit_color", "label_color", *_TEXT_COMMON}),
    "bar": frozenset({"type", "value", "max", "label", "unit", "color", "value_size",
                      "label_size", "value_color", "label_color", *_TEXT_COMMON}),
    "icon": frozenset({"type", "icon", "text", "color", "icon_size", "text_size", "text_color",
                       *_TEXT_COMMON}),
    "image": frozenset({"type", "source", "fit", "zoom", "offset_x", "offset_y", "rotate",
                        "background"}),
    "gif": frozenset({"type", "source", "loop", "background", "x", "y"}),
    "sparkline": frozenset({"type", "entity", "hours", "unit", "color", *_TEXT_COMMON}),
    "graph": frozenset({"type", "entity", "hours", "style", "label", "unit", "show_value",
                        "min", "max", "points", "color", "value_size", "value_color",
                        "label_color", *_TEXT_COMMON}),
    "progress": frozenset({"type", "value", "max", "label", "finish", "eta_format", "color",
                           "track", "value_size", "value_color", "label_size", "label_color",
                           "eta_color", *_TEXT_COMMON}),
    "analog_clock": frozenset({"type", "tz", "seconds", "numerals", "label", "color", "accent",
                               "face", "label_color", *_TEXT_COMMON}),
    "weather": frozenset({"type", "entity", "label", "show", "color", "icon_color",
                          "temp_size", "hl_color", "label_color", *_TEXT_COMMON}),
    "ticker": frozenset({"type", "text", "size", "color", "background", "y", "speed", "label",
                         "label_color", *_TEXT_COMMON}),
    "custom": frozenset({"type", "items", "background"}),
    "blank": frozenset({"type"}),
    # item types (a custom widget's / canvas's items)
    "item:text": frozenset({"type", "text", "x", "y", "size", "weight", "align", "color",
                            "width", "height", "font"}),
    "item:ring": frozenset({"type", "value", "max", "x", "y", "radius", "thickness", "color",
                            "track", "start", "sweep"}),
    "item:bar": frozenset({"type", "value", "max", "x", "y", "width", "height", "direction",
                           "radius", "color", "track"}),
    "item:icon": frozenset({"type", "icon", "x", "y", "size", "color"}),
    "item:image": frozenset({"type", "source", "x", "y", "width", "height", "fit", "zoom",
                             "offset_x", "offset_y", "rotate", "background"}),
    "item:rect": frozenset({"type", "x", "y", "width", "height", "radius", "color"}),
}


def _did_you_mean(key: str, known: frozenset[str]) -> str:
    """' — did you mean 'color'?' for an unknown key, or '' when nothing is close."""
    matches = difflib.get_close_matches(key, sorted(known), n=1, cutoff=0.6)
    return f" — did you mean '{matches[0]}'?" if matches else ""


def _warn_unknown_keys(warnings: list[str], where: str, mapping: dict, known: frozenset[str]) -> None:
    """One warning per key of `mapping` that `known` doesn't list (the mapping still loads)."""
    for key in mapping:
        if isinstance(key, str) and key not in known:
            warnings.append(f"{where}: unknown field '{key}'{_did_you_mean(key, known)}")


@dataclass
class Screen:
    name: str
    duration: int | None = None       # None -> default_duration
    refresh: int | None = None        # None -> default_refresh
    when: str | None = None           # HA template; skipped in rotation unless "true"
    background: str = "#000000"
    transition: str | None = None     # None -> the top-level `transition` default
    transition_delay: int | None = None   # ms between tubes (a flip: between their starts), None -> 90
    transition_duration: int | None = None   # ms a wipe takes, or each tube's flip, None -> 500
    tubes: list[dict] = field(default_factory=list)   # exactly 6 widgets
    strip: dict | None = None         # alternative: one 810x240 picture
    canvas: dict | None = None        # alternative: free design across all six tubes

    def kind(self) -> str:
        """"tubes", "strip" or "canvas" - exactly one of them is set."""
        if self.strip is not None:
            return "strip"
        if self.canvas is not None:
            return "canvas"
        return "tubes"

    def resolved_duration(self, default: int) -> int:
        return self.duration if self.duration is not None else default

    def resolved_refresh(self, default: int) -> int:
        return self.refresh if self.refresh is not None else default

    def resolved_transition(self, default: str) -> str:
        return self.transition if self.transition is not None else default

    def resolved_transition_delay(self, default: int = DEFAULT_TRANSITION_DELAY) -> int:
        return self.transition_delay if self.transition_delay is not None else default

    def resolved_transition_duration(self, default: int = DEFAULT_TRANSITION_DURATION) -> int:
        return self.transition_duration if self.transition_duration is not None else default


@dataclass
class ClockOverride:
    """Per-clock override of the global rotation / clock duration (screens.yaml `clocks:`)."""

    rotation: list[str] | None = None
    clock_duration: int | None = None


@dataclass
class ScreensConfig:
    rotation: list[str] = field(default_factory=list)
    clock_duration: int | None = None   # duration for the "clock" rotation entry
    transition: str = DEFAULT_TRANSITION  # top-level default for screens' `transition`
    font: str | None = None              # top-level default `font:` for all widgets
    screens: dict[str, Screen] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)  # human-readable, per screen
    warnings: list[str] = field(default_factory=list)  # unknown fields etc.; the file still loads
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


def _check_item(errors: list[str], where: str, n: int, it: object,
                warnings: list[str] | None = None) -> bool:
    """One item of a custom widget / canvas (ITEM_TYPES), validated the same way in both."""
    if not isinstance(it, dict) or it.get("type") not in ITEM_TYPES:
        kind = it.get("type") if isinstance(it, dict) else type(it).__name__
        suggestion = _did_you_mean(str(kind), frozenset(ITEM_TYPES))
        errors.append(f"{where} item {n}: unknown item type {kind!r}{suggestion} "
                      f"(expected one of {', '.join(ITEM_TYPES)})")
        return False
    if warnings is not None:
        _warn_unknown_keys(warnings, f"{where} item {n} ({it['type']})", it,
                           KNOWN_FIELDS.get(f"item:{it['type']}", frozenset()))
    if it["type"] == "image" and it.get("fit", "contain") not in FITS:
        errors.append(f"{where} item {n}: image 'fit' must be one of {', '.join(FITS)}")
        return False
    if it["type"] == "icon":
        icon = it.get("icon")
        if not isinstance(icon, str) or not icon.startswith("mdi:"):
            errors.append(f"{where} item {n}: icon item needs 'icon: mdi:<name>'")
            return False
    return True


def _check_widget(errors: list[str], screen: str, idx: int, w: object,
                  warnings: list[str] | None = None) -> bool:
    where = f"screen '{screen}' tube {idx}"
    if not isinstance(w, dict):
        errors.append(f"{where}: widget must be a mapping, got {type(w).__name__}")
        return False
    wtype = w.get("type")
    if wtype not in WIDGET_TYPES:
        suggestion = _did_you_mean(str(wtype), frozenset(WIDGET_TYPES))
        errors.append(f"{where}: unknown widget type {wtype!r}{suggestion} "
                      f"(expected one of {', '.join(WIDGET_TYPES)})")
        return False
    if warnings is not None:
        _warn_unknown_keys(warnings, f"{where} ({wtype})", w, KNOWN_FIELDS.get(str(wtype), frozenset()))
    if wtype == "image":
        src = w.get("source")
        if not isinstance(src, str) or not src:
            errors.append(f"{where}: image widget needs a 'source' (builtin:..., file:<uploaded picture>, http(s)://..., camera:..., entity_picture:...)")
            return False
        fit = w.get("fit", "cover")
        if fit not in FITS:
            errors.append(f"{where}: image 'fit' must be one of {', '.join(FITS)}, got {fit!r}")
            return False
    if wtype == "gif":
        src = w.get("source")
        if not isinstance(src, str) or not src:
            errors.append(f"{where}: gif widget needs a 'source' (file:<uploaded .gif from the picture library>)")
            return False
        if not src.startswith("file:") or not src[len("file:"):].lower().endswith(".gif"):
            errors.append(f"{where}: gif 'source' must be a file:<name>.gif from the picture library, got {src!r}")
            return False
        if "loop" in w and not isinstance(w["loop"], bool):
            errors.append(f"{where}: gif 'loop' must be true or false, got {w['loop']!r}")
            return False
        for k in ("x", "y"):
            if k in w and (isinstance(w[k], bool) or not isinstance(w[k], (int, float))):
                errors.append(f"{where}: gif '{k}' must be a number, got {w[k]!r}")
                return False
    if wtype in ("sparkline", "graph", "weather"):
        if not isinstance(w.get("entity"), str) or not w["entity"]:
            errors.append(f"{where}: {wtype} widget needs an 'entity'")
            return False
    if wtype == "graph":
        style = w.get("style", "area")
        if style not in ("line", "area", "bars"):
            errors.append(f"{where}: graph 'style' must be one of line, area, bars, got {style!r}")
            return False
    if wtype == "progress":
        if "value" not in w:
            errors.append(f"{where}: progress widget needs a 'value'")
            return False
        eta_format = w.get("eta_format", "remaining")
        if eta_format not in ("time", "remaining"):
            errors.append(f"{where}: progress 'eta_format' must be one of time, remaining, got {eta_format!r}")
            return False
    if wtype == "weather":
        show = w.get("show", "today")
        if show not in ("today", "now"):
            errors.append(f"{where}: weather 'show' must be one of today, now, got {show!r}")
            return False
    if wtype == "analog_clock":
        if "seconds" in w and not isinstance(w["seconds"], bool):
            errors.append(f"{where}: analog_clock 'seconds' must be true or false, got {w['seconds']!r}")
            return False
        if "numerals" in w and not isinstance(w["numerals"], bool):
            errors.append(f"{where}: analog_clock 'numerals' must be true or false, got {w['numerals']!r}")
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
            if not _check_item(errors, where, n, it, warnings):
                return False
    return True


def _parse_screen(name: str, raw: object, errors: list[str],
                  warnings: list[str] | None = None) -> Screen | None:
    if not isinstance(raw, dict):
        _err(errors, name, "must be a mapping")
        return None
    if warnings is not None:
        _warn_unknown_keys(warnings, f"screen '{name}'", raw, KNOWN_FIELDS["screen"])
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
    transition = raw.get("transition")
    if transition is not None:
        if transition not in TRANSITIONS:
            # unknown value: report it, but the screen still loads (with "none")
            _err(errors, name, f"unknown 'transition' {transition!r} (expected one of "
                                f"{', '.join(TRANSITIONS)}) - using 'none'")
        else:
            screen.transition = transition
    delay = raw.get("transition_delay")
    if delay is not None:
        if isinstance(delay, bool) or not isinstance(delay, (int, float)) or not 0 <= delay <= 1000:
            _err(errors, name, f"'transition_delay' must be a number of ms in 0..1000, got {delay!r}")
        else:
            screen.transition_delay = int(delay)
    dur = raw.get("transition_duration")
    if dur is not None:
        if isinstance(dur, bool) or not isinstance(dur, (int, float)) or not 100 <= dur <= 5000:
            _err(errors, name, f"'transition_duration' must be a number of ms in 100..5000, got {dur!r}")
        else:
            screen.transition_duration = int(dur)
    bg = raw.get("background", "#000000")
    if not isinstance(bg, str):
        _err(errors, name, f"'background' must be a string, got {type(bg).__name__}")
        return None
    screen.background = bg

    has_tubes = "tubes" in raw
    has_strip = "strip" in raw
    has_canvas = "canvas" in raw
    kinds = [k for k, has in (("tubes", has_tubes), ("strip", has_strip), ("canvas", has_canvas)) if has]
    if len(kinds) > 1:
        _err(errors, name, f"use only one of 'tubes', 'strip', 'canvas' - not both/all of them (got {', '.join(kinds)})")
        return None
    if not kinds:
        _err(errors, name, "needs 'tubes' (6 widgets), 'strip' (one 810x240 picture) or 'canvas' (free design across all six tubes)")
        return None
    if has_strip:
        strip = raw["strip"]
        if not isinstance(strip, dict) or not isinstance(strip.get("source"), str) or not strip["source"]:
            _err(errors, name, "'strip' must be a mapping with a 'source' (camera:..., http(s)://..., ...)")
            return None
        if warnings is not None:
            _warn_unknown_keys(warnings, f"screen '{name}' strip", strip, KNOWN_FIELDS["strip"])
        fit = strip.get("fit", "cover")
        if fit not in FITS:
            _err(errors, name, f"strip 'fit' must be one of {', '.join(FITS)}, got {fit!r}")
            return None
        screen.strip = strip
    elif has_canvas:
        canvas = raw["canvas"]
        if not isinstance(canvas, dict):
            _err(errors, name, "'canvas' must be a mapping with an 'items' list (text/ring/bar/icon/image/rect placed by x/y in 0..810 x 0..240)")
            return None
        if warnings is not None:
            _warn_unknown_keys(warnings, f"screen '{name}' canvas", canvas, KNOWN_FIELDS["canvas"])
        items = canvas.get("items", [])
        if not isinstance(items, list):
            _err(errors, name, "canvas 'items' must be a list")
            return None
        where = f"screen '{name}' canvas"
        for n, it in enumerate(items):
            if not _check_item(errors, where, n, it, warnings):
                return None
        screen.canvas = canvas
    else:
        tubes = raw["tubes"]
        if not isinstance(tubes, list) or len(tubes) != 6:
            n = len(tubes) if isinstance(tubes, list) else type(tubes).__name__
            _err(errors, name, f"'tubes' must be a list of exactly 6 widgets, got {n}")
            return None
        for i, w in enumerate(tubes):
            if not _check_widget(errors, name, i, w, warnings):
                return None
        n_gif = sum(1 for w in tubes if isinstance(w, dict) and w.get("type") == "gif")
        if n_gif > MAX_GIF_TUBES:
            _err(errors, name, f"at most {MAX_GIF_TUBES} gif tubes per screen (the clock plays "
                                f"at most {MAX_GIF_TUBES} GIFs at once), got {n_gif}")
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

    transition = data.get("transition")
    if transition is not None:
        if transition not in TRANSITIONS:
            cfg.errors.append(f"unknown top-level 'transition' {transition!r} (expected one of "
                              f"{', '.join(TRANSITIONS)}) - using 'none'")
        else:
            cfg.transition = transition

    font = data.get("font")
    if font is not None:
        if not isinstance(font, str) or not font:
            cfg.errors.append(f"top-level 'font' must be a non-empty string (a bundled family id or file:<name>), got {font!r}")
        else:
            cfg.font = font
    elif "font" in data:
        cfg.errors.append("top-level 'font' must be a non-empty string (a bundled family id or file:<name>), got None")

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
        screen = _parse_screen(name, raw, cfg.errors, cfg.warnings)
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
# transition: cascade    # optional default for all screens: none | cascade |
#                        # cascade_right | center_out | wipe | wipe_right |
#                        # wipe_center (how a screen appears when it becomes
#                        # active; per screen too, with transition_delay and,
#                        # for the wipes, transition_duration in ms)
# font: bebas-neue      # optional default font for every widget that draws text
#                        # (a bundled family id, or file:<uploaded font>); each
#                        # widget's own `font:` wins over this

screens:
  # A screen is `tubes:` (exactly 6 widgets, left to right, one per 135x240
  # tube), `strip:` (one 810x240 picture across all six tubes) or `canvas:`
  # (a free design across all six tubes: items placed by x/y in 0..810 x 0..240).
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

  # A free design across all six tubes: the same items a custom tube is made
  # of, but x/y are in strip coordinates (0..810 x 0..240).
  weather_wide:
    canvas:
      background: "#000000"
      items:
        - {type: icon, icon: "mdi:weather-partly-cloudy", x: 67, y: 120, size: 100}
        - {type: text, text: "{{ states('sensor.outside_temp') }}\u00b0", x: 202, y: 120, size: 120, font: bebas-neue}
        - {type: bar, value: "{{ states('sensor.humidity') }}", max: 100, x: 607, y: 200, width: 380, height: 12}

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
#   graph      entity + hours, bigger history graph: style line|area|bars, optional label,
#              unit, show_value, fixed min/max axis, points (bar buckets / max line points)
#   progress   value 0..max with an ETA: finish (ISO datetime, unix timestamp or HH:MM),
#              eta_format time|remaining, label, color, track - a 270 degree gauge
#   analog_clock  analogue clock face: tz, seconds, numerals (12/3/6/9), label, color,
#              accent (seconds hand), face
#   weather    weather.* entity: condition icon + temperature, show today|now (today adds
#              the daily high/low), label, color, icon_color
#   ticker     scrolling text band: text (template), size, color, background, y, speed,
#              label
#   custom     items: a list of text / ring / bar / icon / image / rect, each placed by x, y
#              (easiest with the editor's designer)
#   blank      empty tube
# Every widget that draws text also takes `font:` (a bundled family id like
# bebas-neue, or file:<uploaded font>); the top-level `font:` is the default.
# Colours accept "#RRGGBB" or a template returning one.
"""
