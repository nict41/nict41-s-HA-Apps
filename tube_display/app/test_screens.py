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
