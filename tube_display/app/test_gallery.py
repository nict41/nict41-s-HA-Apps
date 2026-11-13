"""Gallery templates (gallery.py): build() with placeholders, validation, optional
tubes, and suggest() from a fake HA /api/states list."""

import pytest

import gallery
from screens import parse_screens


def _values(t):
    """A valid entity id for every input of template t (optional ones included)."""
    return {i["key"]: i["domain"] + ".test_" + i["key"] for i in t["inputs"]}


def test_every_template_builds_and_parses_clean():
    for t in gallery.GALLERY:
        screen = gallery.build(t["id"], _values(t))
        assert "__optional__" not in str(screen)
        cfg = parse_screens({"rotation": ["s"], "screens": {"s": screen}})
        assert cfg.errors == [], (t["id"], cfg.errors)
        assert cfg.warnings == [], (t["id"], cfg.warnings)


def test_list_templates_has_no_screen_bodies():
    out = gallery.list_templates()
    assert [t["id"] for t in out] == [t["id"] for t in gallery.GALLERY]
    for t in out:
        assert set(t) == {"id", "name", "description", "inputs"}
        for i in t["inputs"]:
            assert {"key", "label", "domain"} <= set(i)


def test_placeholders_all_replaced():
    screen = gallery.build("weather", {"weather": "weather.home", "temp": "sensor.outdoor"})
    text = str(screen)
    assert "[[" not in text and "]]" not in text
    assert "weather.home" in text
    assert "sensor.outdoor" in text


def test_optional_input_empty_becomes_blank_tube():
    screen = gallery.build("weather", {"weather": "weather.home", "temp": ""})
    types = [w["type"] for w in screen["tubes"]]
    assert "blank" in types          # the graph tube went blank
    assert "weather" in types
    # and with the sensor filled in, the graph is back
    screen2 = gallery.build("weather", {"weather": "weather.home", "temp": "sensor.outdoor"})
    assert "graph" in [w["type"] for w in screen2["tubes"]]


def test_missing_required_input_raises():
    with pytest.raises(ValueError, match="missing input"):
        gallery.build("weather", {"temp": "sensor.outdoor"})
    with pytest.raises(ValueError, match="missing input"):
        gallery.build("now_playing", {})


def test_bad_entity_id_raises():
    with pytest.raises(ValueError, match="not a valid entity id"):
        gallery.build("now_playing", {"player": "Not An Entity"})
    with pytest.raises(ValueError, match="not a valid entity id"):
        gallery.build("now_playing", {"player": "media_player"})


def test_unknown_template_raises():
    with pytest.raises(ValueError, match="unknown gallery template"):
        gallery.build("nope", {})


def test_every_placeholder_is_a_declared_input():
    for t in gallery.GALLERY:
        declared = {i["key"] for i in t["inputs"]}
        for ph in gallery._placeholders(t["screen"]):
            assert ph in declared, (t["id"], ph)


def test_now_playing_uses_entity_picture():
    screen = gallery.build("now_playing", {"player": "media_player.spotify"})
    sources = [w.get("source") for w in screen["tubes"] if w.get("type") == "image"]
    assert sources == ["entity_picture:media_player.spotify"]


def test_doorbell_is_a_strip_camera():
    screen = gallery.build("doorbell", {"camera": "camera.front"})
    assert screen["strip"]["source"] == "camera:camera.front"


# --------------------------------------------------------------------- suggest

FAKE_STATES = [
    {"entity_id": "weather.home", "state": "sunny", "attributes": {"friendly_name": "Home"}},
    {"entity_id": "sensor.outdoor_temp", "state": "12.3",
     "attributes": {"device_class": "temperature", "unit_of_measurement": "°C"}},
    {"entity_id": "media_player.living_room", "state": "playing", "attributes": {}},
    {"entity_id": "media_player.kitchen", "state": "idle", "attributes": {}},
    {"entity_id": "media_player.bedroom", "state": "off", "attributes": {}},
    {"entity_id": "sensor.power_now", "state": "350",
     "attributes": {"device_class": "power", "unit_of_measurement": "W"}},
    {"entity_id": "sensor.energy_today", "state": "4.2",
     "attributes": {"device_class": "energy", "unit_of_measurement": "kWh"}},
    {"entity_id": "sensor.printer_print_progress", "state": "42",
     "attributes": {"unit_of_measurement": "%"}},
    {"entity_id": "sensor.printer_finish_time", "state": "2026-11-13T22:00:00",
     "attributes": {}},
    {"entity_id": "sensor.printer_nozzle_temp", "state": "210",
     "attributes": {"device_class": "temperature"}},
    {"entity_id": "sensor.printer_bed_temp", "state": "60",
     "attributes": {"device_class": "temperature"}},
    {"entity_id": "calendar.family", "state": "on", "attributes": {}},
    {"entity_id": "camera.doorbell", "state": "streaming", "attributes": {}},
    {"entity_id": "light.living_room", "state": "on", "attributes": {}},
]


def test_suggest_finds_everything():
    out = gallery.suggest(FAKE_STATES)
    ids = [s["id"] for s in out]
    assert "clock" in ids                      # always
    assert "weather" in ids
    assert "now_playing" in ids
    assert "energy" in ids
    assert "printer" in ids
    assert "next_event" in ids
    assert "doorbell" in ids
    # at most 2 media players
    assert ids.count("now_playing") == 2
    by_id = {}
    for s in out:
        by_id.setdefault(s["id"], []).append(s)
    assert by_id["weather"][0]["values"]["weather"] == "weather.home"
    assert by_id["weather"][0]["values"]["temp"] == "sensor.outdoor_temp"
    assert by_id["now_playing"][0]["values"]["player"] == "media_player.living_room"
    assert by_id["energy"][0]["values"] == {"power": "sensor.power_now",
                                            "energy": "sensor.energy_today"}
    printer = by_id["printer"][0]["values"]
    assert printer["progress"] == "sensor.printer_print_progress"
    assert printer["finish"] == "sensor.printer_finish_time"
    assert printer["nozzle"] == "sensor.printer_nozzle_temp"
    assert printer["bed"] == "sensor.printer_bed_temp"
    assert by_id["next_event"][0]["values"]["calendar"] == "calendar.family"
    assert by_id["doorbell"][0]["values"]["camera"] == "camera.doorbell"


def test_suggested_values_build_valid_screens():
    for s in gallery.suggest(FAKE_STATES):
        screen = gallery.build(s["id"], s["values"])
        cfg = parse_screens({"rotation": ["s"], "screens": {"s": screen}})
        assert cfg.errors == [], (s["id"], cfg.errors)


def test_suggest_empty_states_only_clock():
    out = gallery.suggest([])
    assert [s["id"] for s in out] == ["clock"]
    assert gallery.suggest(None) == out
    assert gallery.suggest("not a list") == out


def test_suggest_partial_entities():
    out = gallery.suggest([
        {"entity_id": "weather.home", "state": "sunny", "attributes": {}},
        {"entity_id": "sensor.power_now", "state": "10",
         "attributes": {"device_class": "power"}},
    ])
    ids = [s["id"] for s in out]
    assert ids == ["clock", "weather", "energy"]
    # no energy sensor -> the energy input is simply not pre-filled
    energy = [s for s in out if s["id"] == "energy"][0]
    assert energy["values"] == {"power": "sensor.power_now"}
    # ... and building without it fails (it is a required input)
    with pytest.raises(ValueError):
        gallery.build("energy", energy["values"])
