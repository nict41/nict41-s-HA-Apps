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
