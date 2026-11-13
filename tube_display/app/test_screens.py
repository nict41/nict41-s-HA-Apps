"""screens.yaml parsing and validation."""

import pytest
import yaml

from screens import CLOCK_ENTRY, ScreensError, parse_screens


def _cfg(**kwargs):
    base = {
        "rotation": ["a"],
        "screens": {"a": {"tubes": [{"type": "blank"}] * 6}},
    }
    base.update(kwargs)
    return base


def test_valid_minimal():
    cfg = parse_screens(_cfg())
    assert cfg.errors == []
    assert cfg.rotation == ["a"]
    assert "a" in cfg.screens
    assert cfg.screens["a"].resolved_duration(15) == 15


def test_valid_full_example():
    data = yaml.safe_load("""
rotation: [claude, clock]
clock: {duration: 60}
screens:
  claude:
    duration: 20
    refresh: 30
    when: "{{ is_state('binary_sensor.x','on') }}"
    background: "#000000"
    tubes:
      - {type: image, source: "builtin:claude"}
      - {type: ring, value: "{{ states('sensor.x') }}", max: 100, label: "Session", unit: "%", color: "#D97757"}
      - {type: ring, value: "1", max: 100, label: "Week"}
      - {type: text, text: "a\\nb", size: auto, color: "#FFFFFF", align: center}
      - {type: big_number, value: "5", unit: "%", label: "Pace"}
      - {type: icon, icon: "mdi:robot", text: "hi", color: "#D97757"}
  doorbell:
    strip: {source: "camera:camera.front_door"}
""")
    # rotation references "doorbell"? no - fix rotation to include it
    data["rotation"] = ["claude", "doorbell", "clock"]
    cfg = parse_screens(data)
    assert cfg.errors == []
    assert cfg.clock_duration == 60
    assert cfg.screens["claude"].duration == 20
    assert cfg.screens["claude"].refresh == 30
    assert cfg.screens["claude"].when == "{{ is_state('binary_sensor.x','on') }}"
    assert cfg.screens["doorbell"].strip["source"] == "camera:camera.front_door"


def test_not_a_mapping():
    with pytest.raises(ScreensError):
        parse_screens(["not", "a", "mapping"])


def test_empty_rotation():
    with pytest.raises(ScreensError):
        parse_screens({"rotation": [], "screens": {"a": {"tubes": [{"type": "blank"}] * 6}}})


def test_missing_screens():
    with pytest.raises(ScreensError):
        parse_screens({"rotation": ["a"]})


def test_rotation_entry_without_screen_is_error_but_rest_works():
    cfg = parse_screens(_cfg(rotation=["a", "ghost"]))
    assert "a" in cfg.screens
    assert any("ghost" in e for e in cfg.errors)


def test_wrong_tube_count():
    cfg = parse_screens(_cfg(screens={"a": {"tubes": [{"type": "blank"}] * 5}}))
    assert cfg.screens == {}
    assert any("exactly 6" in e for e in cfg.errors)


def test_unknown_widget_type():
    tubes = [{"type": "blank"}] * 5 + [{"type": "hologram"}]
    cfg = parse_screens(_cfg(screens={"a": {"tubes": tubes}}))
    assert any("unknown widget type" in e for e in cfg.errors)
    assert "a" not in cfg.screens


def test_tubes_and_strip_both_rejected():
    cfg = parse_screens(_cfg(screens={"a": {"tubes": [{"type": "blank"}] * 6,
                                            "strip": {"source": "camera:x"}}}))
    assert any("not both" in e for e in cfg.errors)


def test_neither_tubes_nor_strip():
    cfg = parse_screens(_cfg(screens={"a": {"background": "#000"}}))
    assert any("needs 'tubes'" in e for e in cfg.errors)


def test_bad_duration():
    cfg = parse_screens(_cfg(screens={"a": {"duration": -5, "tubes": [{"type": "blank"}] * 6}}))
    assert any("duration" in e for e in cfg.errors)


def test_image_needs_source():
    tubes = [{"type": "blank"}] * 5 + [{"type": "image"}]
    cfg = parse_screens(_cfg(screens={"a": {"tubes": tubes}}))
    assert any("source" in e for e in cfg.errors)


def test_icon_needs_mdi_prefix():
    tubes = [{"type": "blank"}] * 5 + [{"type": "icon", "icon": "robot"}]
    cfg = parse_screens(_cfg(screens={"a": {"tubes": tubes}}))
    assert any("mdi:" in e for e in cfg.errors)


def test_sparkline_needs_entity():
    tubes = [{"type": "blank"}] * 5 + [{"type": "sparkline", "hours": 24}]
    cfg = parse_screens(_cfg(screens={"a": {"tubes": tubes}}))
    assert any("entity" in e for e in cfg.errors)


def test_new_widget_types_accepted():
    from screens import WIDGET_TYPES
    for t in ("graph", "progress", "analog_clock", "weather", "ticker", "gif"):
        assert t in WIDGET_TYPES
    # one new type per screen (a screen has exactly 6 tubes)
    widgets = [
        {"type": "graph", "entity": "sensor.temp"},
        {"type": "progress", "value": "42", "max": 100, "finish": "18:30", "eta_format": "time"},
        {"type": "analog_clock", "tz": "Europe/Berlin", "seconds": True, "numerals": False},
        {"type": "weather", "entity": "weather.home", "show": "now"},
        {"type": "ticker", "text": "hello", "size": 28, "speed": 40},
    ]
    for w in widgets:
        tubes = [{"type": "blank"}] * 5 + [w]
        cfg = parse_screens(_cfg(screens={"a": {"tubes": tubes}}))
        assert cfg.errors == [], (w["type"], cfg.errors)


def test_graph_weather_need_entity():
    for t in ("graph", "weather"):
        tubes = [{"type": "blank"}] * 5 + [{"type": t}]
        cfg = parse_screens(_cfg(screens={"a": {"tubes": tubes}}))
        assert any("entity" in e for e in cfg.errors), t


def test_graph_bad_style_rejected():
    tubes = [{"type": "blank"}] * 5 + [{"type": "graph", "entity": "sensor.x", "style": "pie"}]
    cfg = parse_screens(_cfg(screens={"a": {"tubes": tubes}}))
    assert any("style" in e and "pie" in e for e in cfg.errors)


def test_progress_needs_value_and_valid_eta_format():
    tubes = [{"type": "blank"}] * 5 + [{"type": "progress", "max": 100}]
    cfg = parse_screens(_cfg(screens={"a": {"tubes": tubes}}))
    assert any("value" in e for e in cfg.errors)
    tubes = [{"type": "blank"}] * 5 + [{"type": "progress", "value": "5", "eta_format": "soon"}]
    cfg = parse_screens(_cfg(screens={"a": {"tubes": tubes}}))
    assert any("eta_format" in e for e in cfg.errors)


def test_weather_bad_show_rejected():
    tubes = [{"type": "blank"}] * 5 + [{"type": "weather", "entity": "weather.x", "show": "week"}]
    cfg = parse_screens(_cfg(screens={"a": {"tubes": tubes}}))
    assert any("show" in e for e in cfg.errors)


def test_analog_clock_bool_fields_rejected_when_not_bool():
    for f in ("seconds", "numerals"):
        tubes = [{"type": "blank"}] * 5 + [{"type": "analog_clock", f: "yes"}]
        cfg = parse_screens(_cfg(screens={"a": {"tubes": tubes}}))
        assert any(f in e for e in cfg.errors), f


def test_bad_strip_fit():
    cfg = parse_screens(_cfg(screens={"a": {"strip": {"source": "camera:x", "fit": "squish"}}}))
    assert any("fit" in e for e in cfg.errors)


def test_bad_clock_duration():
    with pytest.raises(ScreensError):
        parse_screens(_cfg(clock={"duration": "soon"}))


def test_clock_entry_constant():
    assert CLOCK_ENTRY == "clock"


def test_custom_widget_items_validated():
    ok = [{"type": "blank"}] * 5 + [{"type": "custom", "items": [
        {"type": "text", "text": "hi", "x": 10, "y": 20}, {"type": "ring", "value": "5"}]}]
    assert parse_screens(_cfg(screens={"a": {"tubes": ok}})).errors == []
    empty = [{"type": "blank"}] * 5 + [{"type": "custom"}]
    assert parse_screens(_cfg(screens={"a": {"tubes": empty}})).errors == []
    bad = [{"type": "blank"}] * 5 + [{"type": "custom", "items": [{"type": "sparkle"}]}]
    assert any("item 0" in e and "sparkle" in e for e in parse_screens(_cfg(screens={"a": {"tubes": bad}})).errors)
    not_list = [{"type": "blank"}] * 5 + [{"type": "custom", "items": "text"}]
    assert any("must be a list" in e for e in parse_screens(_cfg(screens={"a": {"tubes": not_list}})).errors)


# ----------------------------------------------------------------------
# per-clock overrides (`clocks:`) and the `night:` block
# ----------------------------------------------------------------------

def test_clock_overrides_parsed_and_fallback():
    base = _cfg(rotation=["a"], screens={
        "a": {"tubes": [{"type": "blank"}] * 6},
        "b": {"tubes": [{"type": "blank"}] * 6},
    })
    base["rotation"] = ["a", "b"]
    base["clocks"] = {"kitchen": {"rotation": ["b"], "clock": {"duration": 90}},
                      "main": {"clock": {"duration": 5}}}
    cfg = parse_screens(base)
    assert cfg.errors == []
    assert cfg.rotation_for("main") == ["a", "b"]            # global fallback
    assert cfg.rotation_for("kitchen") == ["b"]              # its own override
    assert cfg.clock_duration_for("main") == 5
    assert cfg.clock_duration_for("kitchen") == 90
    assert cfg.clock_duration_for("unknown") is None          # global clock.duration unset
    cfg2 = parse_screens(_cfg(clock={"duration": 60}, clocks={"k": {"rotation": ["a"]}}))
    assert cfg2.clock_duration_for("k") == 60                 # falls back to global clock
    assert cfg2.rotation_for("k") == ["a"]


def test_clock_overrides_bad_entries_are_errors_not_exceptions():
    cfg = parse_screens(_cfg(clocks={
        "bad1": {"rotation": "notalist"},
        "bad2": {"rotation": []},
        "bad3": {"rotation": ["ghost"]},          # no such screen
        "bad4": {"clock": {"duration": -3}},
        "bad5": "not a mapping",
        "good": {"rotation": ["a"]},
    }))
    assert cfg.rotation_for("good") == ["a"]
    assert cfg.rotation_for("bad1") == ["a"]      # bad override ignored -> global
    assert len(cfg.errors) >= 5
    assert any("bad1" in e and "rotation" in e for e in cfg.errors)
    assert any("bad2" in e for e in cfg.errors)
    assert any("bad3" in e and "ghost" in e for e in cfg.errors)
    assert any("bad4" in e and "duration" in e for e in cfg.errors)
    assert any("bad5" in e for e in cfg.errors)


def test_clocks_not_a_mapping_is_error():
    cfg = parse_screens(_cfg(clocks=["main"]))
    assert any("'clocks' must be a mapping" in e for e in cfg.errors)


def test_night_block_parsed():
    cfg = parse_screens(_cfg(night={
        "brightness_day": 200, "brightness_night": 40,
        "bright_above": 4, "dim_below": -5, "dim_screens": 0.7,
        "quiet": {"from": "23:00", "to": "07:00", "mode": "blank"},
    }))
    assert cfg.errors == []
    assert cfg.night is not None
    assert cfg.night.brightness_day == 200 and cfg.night.brightness_night == 40
    assert cfg.night.dim_screens == 0.7
    assert cfg.night.quiet.mode == "blank"
    assert cfg.night.quiet.start.hour == 23 and cfg.night.quiet.end.hour == 7


def test_night_absent_means_none():
    cfg = parse_screens(_cfg())
    assert cfg.night is None
    assert cfg.errors == []


def test_night_block_errors_go_to_cfg_errors():
    cfg = parse_screens(_cfg(night={"brightness_day": 1, "quiet": {"from": "nope", "to": "07:00"}}))
    assert cfg.night is None
    assert any("brightness_day" in e for e in cfg.errors)
    assert any("quiet.from" in e for e in cfg.errors)


# ----------------------------------------------------------------------
# transition / transition_delay fields
# ----------------------------------------------------------------------

def test_transition_defaults_to_none():
    cfg = parse_screens(_cfg())
    assert cfg.transition == "none"
    assert cfg.screens["a"].transition is None
    assert cfg.screens["a"].resolved_transition(cfg.transition) == "none"
    assert cfg.screens["a"].resolved_transition_delay() == 90


def test_transition_per_screen_and_top_level():
    cfg = parse_screens(_cfg(
        transition="cascade",
        screens={"a": {"transition": "wipe", "transition_delay": 200,
                       "tubes": [{"type": "blank"}] * 6},
                 "b": {"tubes": [{"type": "blank"}] * 6}}))
    assert cfg.errors == []
    assert cfg.transition == "cascade"
    assert cfg.screens["a"].resolved_transition(cfg.transition) == "wipe"
    assert cfg.screens["a"].resolved_transition_delay() == 200
    assert cfg.screens["b"].resolved_transition(cfg.transition) == "cascade"  # top-level default
    assert cfg.screens["b"].resolved_transition_delay() == 90


def test_transition_unknown_value_is_error_but_screen_loads_with_none():
    cfg = parse_screens(_cfg(screens={"a": {"transition": "teleport",
                                            "tubes": [{"type": "blank"}] * 6}}))
    assert "a" in cfg.screens                       # the screen still loads
    assert cfg.screens["a"].transition is None      # ... with none
    assert cfg.screens["a"].resolved_transition("cascade") == "cascade"  # falls back to default
    assert any("transition" in e and "teleport" in e for e in cfg.errors)


def test_transition_unknown_top_level_is_error_and_default_none():
    cfg = parse_screens(_cfg(transition="zoom"))
    assert cfg.transition == "none"
    assert any("transition" in e and "zoom" in e for e in cfg.errors)


def test_transition_delay_validated():
    cfg = parse_screens(_cfg(screens={"a": {"transition_delay": 1500,
                                            "tubes": [{"type": "blank"}] * 6}}))
    assert any("transition_delay" in e for e in cfg.errors)
    assert cfg.screens["a"].transition_delay is None
    cfg = parse_screens(_cfg(screens={"a": {"transition_delay": 0,
                                            "tubes": [{"type": "blank"}] * 6}}))
    assert cfg.errors == []
    assert cfg.screens["a"].resolved_transition_delay() == 0


# ----------------------------------------------------------------------
# canvas screens and the top-level `font:` default
# ----------------------------------------------------------------------

CANVAS = {"background": "#000000", "items": [
    {"type": "text", "text": "21.5°", "x": 202, "y": 120, "size": 120, "font": "bebas-neue"},
    {"type": "icon", "icon": "mdi:weather-partly-cloudy", "x": 67, "y": 120, "size": 100},
    {"type": "bar", "value": "60", "max": 100, "x": 607, "y": 200, "width": 380, "height": 12},
]}


def test_canvas_screen_parses():
    cfg = parse_screens(_cfg(screens={"a": {"canvas": CANVAS, "duration": 20, "refresh": 5,
                                            "when": "{{ is_state('sun.sun','above_horizon') }}",
                                            "transition": "wipe"}}))
    assert cfg.errors == []
    s = cfg.screens["a"]
    assert s.kind() == "canvas"
    assert s.canvas == CANVAS
    assert s.resolved_duration(15) == 20 and s.resolved_refresh(30) == 5
    assert s.when and s.resolved_transition("cascade") == "wipe"


def test_canvas_and_tubes_rejected():
    cfg = parse_screens(_cfg(screens={"a": {"tubes": [{"type": "blank"}] * 6, "canvas": CANVAS}}))
    assert "a" not in cfg.screens
    assert any("not both" in e and "canvas" in e for e in cfg.errors)


def test_canvas_and_strip_rejected():
    cfg = parse_screens(_cfg(screens={"a": {"strip": {"source": "camera:x"}, "canvas": CANVAS}}))
    assert "a" not in cfg.screens
    assert any("not both" in e for e in cfg.errors)


def test_canvas_bad_item_rejected():
    bad = {"canvas": {"items": [{"type": "text", "text": "hi"}, {"type": "nope", "x": 5}]}}
    cfg = parse_screens(_cfg(screens={"a": bad}))
    assert "a" not in cfg.screens
    assert any("unknown item type" in e and "canvas" in e for e in cfg.errors)
    # items are validated exactly like custom-tube items (same fields)
    bad2 = {"canvas": {"items": [{"type": "icon", "x": 5}]}}          # icon needs mdi: prefix
    cfg = parse_screens(_cfg(screens={"a": bad2}))
    assert any("mdi:" in e for e in cfg.errors)
    bad3 = {"canvas": {"items": "nope"}}
    cfg = parse_screens(_cfg(screens={"a": bad3}))
    assert any("'items' must be a list" in e for e in cfg.errors)
    bad4 = {"canvas": ["not", "a", "map"]}
    cfg = parse_screens(_cfg(screens={"a": bad4}))
    assert any("'canvas' must be a mapping" in e for e in cfg.errors)


def test_canvas_empty_items_ok():
    cfg = parse_screens(_cfg(screens={"a": {"canvas": {"items": []}}}))
    assert cfg.errors == []
    assert cfg.screens["a"].canvas == {"items": []}


def test_top_level_font_parsed():
    cfg = parse_screens(_cfg(font="bebas-neue"))
    assert cfg.errors == []
    assert cfg.font == "bebas-neue"
    cfg = parse_screens(_cfg(font="file:MyFont.ttf"))
    assert cfg.font == "file:MyFont.ttf" and cfg.errors == []
    cfg = parse_screens(_cfg())                       # absent -> None
    assert cfg.font is None and cfg.errors == []


def test_top_level_font_bad_type_is_error():
    cfg = parse_screens(_cfg(font=42))
    assert cfg.font is None
    assert any("'font' must be a non-empty string" in e for e in cfg.errors)
    cfg = parse_screens(_cfg(font=""))
    assert any("'font' must be a non-empty string" in e for e in cfg.errors)
    cfg = parse_screens(_cfg(font=None))
    assert any("'font' must be a non-empty string" in e for e in cfg.errors)


def test_widget_font_field_accepted_on_text_widgets():
    cfg = parse_screens(_cfg(screens={"a": {"tubes": [
        {"type": "text", "text": "hi", "font": "orbitron"},
        {"type": "big_number", "value": "1", "font": "file:X.ttf"},
        {"type": "custom", "items": [{"type": "text", "text": "x", "font": "nunito"}]},
        {"type": "blank"}, {"type": "blank"}, {"type": "blank"}]}}))
    assert cfg.errors == []
    assert cfg.screens["a"].tubes[0]["font"] == "orbitron"
    assert cfg.screens["a"].tubes[2]["items"][0]["font"] == "nunito"


# ----------------------------------------------------------------------
# warnings: unknown keys get a "did you mean" (the screen still loads)
# ----------------------------------------------------------------------

def test_unknown_widget_field_warns_did_you_mean():
    cfg = parse_screens(_cfg(screens={"a": {"tubes": [
        {"type": "ring", "value": "1", "max": 100, "colour": "#fff"}] + [{"type": "blank"}] * 5}}))
    assert cfg.errors == []                      # still loads
    assert len(cfg.warnings) == 1
    assert "screen 'a' tube 0 (ring): unknown field 'colour'" in cfg.warnings[0]
    assert "did you mean 'color'?" in cfg.warnings[0]


def test_unknown_screen_level_field_warns():
    cfg = parse_screens(_cfg(screens={"a": {"durations": 10, "tubes": [{"type": "blank"}] * 6}}))
    assert cfg.errors == []
    assert any("screen 'a': unknown field 'durations'" in w and "did you mean 'duration'?" in w
               for w in cfg.warnings)


def test_unknown_strip_and_canvas_fields_warn():
    cfg = parse_screens(_cfg(screens={"a": {"strip": {"source": "camera:x", "fits": "cover"}}}))
    assert cfg.errors == []
    assert any("strip" in w and "unknown field 'fits'" in w and "did you mean 'fit'?" in w
               for w in cfg.warnings)
    cfg = parse_screens(_cfg(screens={"a": {"canvas": {"background": "#000", "itemz": []}}}))
    assert any("canvas" in w and "unknown field 'itemz'" in w for w in cfg.warnings)


def test_unknown_item_field_warns():
    cfg = parse_screens(_cfg(screens={"a": {"tubes": [
        {"type": "custom", "items": [{"type": "rect", "x": 1, "y": 2, "width": 3, "height": 4,
                                       "colr": "#fff"}]}] + [{"type": "blank"}] * 5}}))
    assert cfg.errors == []
    assert any("item 0 (rect)" in w and "unknown field 'colr'" in w and "did you mean 'color'?" in w
               for w in cfg.warnings)


def test_unknown_field_without_close_match_warns_without_suggestion():
    cfg = parse_screens(_cfg(screens={"a": {"tubes": [
        {"type": "text", "text": "x", "zzzzqqqq": 1}] + [{"type": "blank"}] * 5}}))
    assert cfg.errors == []
    assert any("unknown field 'zzzzqqqq'" in w and "did you mean" not in w for w in cfg.warnings)


def test_unknown_widget_type_error_has_did_you_mean():
    cfg = parse_screens(_cfg(screens={"a": {"tubes": [{"type": "rign", "value": "1"}] + [{"type": "blank"}] * 5}}))
    assert cfg.errors
    assert any("unknown widget type 'rign'" in e and "did you mean 'ring'?" in e for e in cfg.errors)


def test_unknown_item_type_error_has_did_you_mean():
    cfg = parse_screens(_cfg(screens={"a": {"tubes": [
        {"type": "custom", "items": [{"type": "rec", "x": 1}]}] + [{"type": "blank"}] * 5}}))
    assert cfg.errors
    assert any("unknown item type 'rec'" in e and "did you mean 'rect'?" in e for e in cfg.errors)


def test_known_fields_cover_every_editor_field():
    """Every field the editor's FIELDS / MORE / ITEM_FIELDS tables offer must be in
    KNOWN_FIELDS (parse editor.js with a regex, like the editor itself is plain JS)."""
    import re
    from pathlib import Path
    from screens import KNOWN_FIELDS

    js = (Path(__file__).resolve().parent / "editor_static" / "editor.js").read_text(encoding="utf-8")

    def table(name):
        m = re.search(r"const " + name + r" = \{(.*?)\n\};", js, re.S)
        assert m, name
        out = {}
        for wm in re.finditer(r"(\w+):\s*\[([^\]]*)\]", m.group(1)):
            fields = re.findall(r'"([^"]+)"', wm.group(2))
            out[wm.group(1)] = fields
        return out

    fields, more, item_fields = table("FIELDS"), table("MORE"), table("ITEM_FIELDS")
    for wtype, fl in fields.items():
        known = KNOWN_FIELDS[wtype]
        missing = [f for f in fl if f not in known]
        assert not missing, (wtype, missing)
    for wtype, fl in more.items():
        if wtype not in KNOWN_FIELDS:
            continue
        missing = [f for f in fl if f not in KNOWN_FIELDS[wtype]]
        assert not missing, (wtype, missing)
    for itype, fl in item_fields.items():
        known = KNOWN_FIELDS["item:" + itype]
        missing = [f for f in fl if f not in known]
        assert not missing, (itype, missing)
    # every widget type the editor offers is a known widget type
    wtypes = re.search(r"const WIDGET_TYPES = \[([^\]]*)\]", js).group(1)
    for t in re.findall(r'"([^"]+)"', wtypes):
        assert t in KNOWN_FIELDS, t


# ======================================================================
# gif widget validation
# ======================================================================

def _gif_tubes(n, **extra):
    return [{"type": "gif", "source": "file:party.gif", **extra}] * n + \
           [{"type": "blank"}] * (6 - n)


def test_gif_widget_accepted():
    cfg = parse_screens(_cfg(screens={"a": {"tubes": _gif_tubes(1, loop=False,
                                                                background="#112233", x=10, y=20)}}))
    assert cfg.errors == []
    w = cfg.screens["a"].tubes[0]
    assert w["type"] == "gif" and w["source"] == "file:party.gif"
    assert w["loop"] is False and w["background"] == "#112233" and w["x"] == 10 and w["y"] == 20


def test_gif_two_tubes_ok_three_is_error():
    cfg = parse_screens(_cfg(screens={"a": {"tubes": _gif_tubes(2)}}))
    assert cfg.errors == []
    cfg = parse_screens(_cfg(screens={"a": {"tubes": _gif_tubes(3)}}))
    assert any("at most 2 gif tubes" in e for e in cfg.errors)
    assert "a" not in cfg.screens          # the bad screen is skipped


def test_gif_source_must_be_a_file_gif():
    for bad in ("builtin:claude", "camera:camera.x", "file:photo.png", "http://x/y.gif", "party.gif", ""):
        tubes = [{"type": "blank"}] * 5 + [{"type": "gif", "source": bad}]
        cfg = parse_screens(_cfg(screens={"a": {"tubes": tubes}}))
        assert any("gif" in e and "file:" in e for e in cfg.errors), bad
    # no source at all
    tubes = [{"type": "blank"}] * 5 + [{"type": "gif"}]
    cfg = parse_screens(_cfg(screens={"a": {"tubes": tubes}}))
    assert any("gif widget needs a 'source'" in e for e in cfg.errors)


def test_gif_loop_and_xy_validated():
    tubes = [{"type": "blank"}] * 5 + [{"type": "gif", "source": "file:a.gif", "loop": "yes"}]
    cfg = parse_screens(_cfg(screens={"a": {"tubes": tubes}}))
    assert any("'loop' must be true or false" in e for e in cfg.errors)
    tubes = [{"type": "blank"}] * 5 + [{"type": "gif", "source": "file:a.gif", "x": "left"}]
    cfg = parse_screens(_cfg(screens={"a": {"tubes": tubes}}))
    assert any("'x' must be a number" in e for e in cfg.errors)


def test_gif_unknown_field_warns():
    tubes = [{"type": "blank"}] * 5 + [{"type": "gif", "source": "file:a.gif", "zoom": 2}]
    cfg = parse_screens(_cfg(screens={"a": {"tubes": tubes}}))
    assert cfg.errors == []
    assert any("unknown field 'zoom'" in w for w in cfg.warnings)
