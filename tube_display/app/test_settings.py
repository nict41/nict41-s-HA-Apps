"""settings.py: legacy single clock, TUBE_CLOCKS_JSON, errors, defaults - no Pillow."""

import pytest

from settings import ClockSettings, Settings, load_settings


def _env(**kw):
    base = {
        "TUBE_CLOCK_HOST": "clock.local",
        "TUBE_CLOCK_MQTT_ID": "abc123",
    }
    base.update(kw)
    return base


def test_legacy_single_clock():
    s = load_settings(_env())
    assert s.clock_host == "clock.local"
    assert s.clock_base_url == "http://clock.local"
    assert len(s.clocks) == 1
    c = s.clocks[0]
    assert c.name == "main" and c.host == "clock.local" and c.mqtt_id == "abc123"
    assert c.base_url == "http://clock.local"
    assert s.primary_clock is c
    assert s.errors == []
    # new fields default sensibly
    assert s.app_url == ""
    assert s.firmware_updates is True
    assert s.firmware_variant == "ipstube_ha"


def test_legacy_host_with_scheme_and_user():
    s = load_settings(_env(TUBE_CLOCK_HOST="https://clock.local:8443/",
                           TUBE_CLOCK_USER="u", TUBE_CLOCK_PASSWORD="p"))
    assert s.clocks[0].base_url == "https://clock.local:8443"
    assert s.clocks[0].user == "u" and s.clocks[0].password == "p"


def test_no_clock_host_no_clocks():
    s = load_settings({"TUBE_CLOCK_MQTT_ID": "x"})
    assert s.clocks == []
    assert s.primary_clock is None


def test_clocks_json_adds_clocks_after_legacy():
    import json
    s = load_settings(_env(TUBE_CLOCKS_JSON=json.dumps([
        {"name": "kitchen", "host": "10.0.0.5", "mqtt_id": "k1", "user": "u"},
        {"name": "Bedroom", "host": "http://10.0.0.6", "mqtt_id": "b1"},
    ])))
    assert [c.name for c in s.clocks] == ["main", "kitchen", "bedroom"]  # lower-cased
    assert s.clocks[1].base_url == "http://10.0.0.5" and s.clocks[1].user == "u"
    assert s.clocks[2].base_url == "http://10.0.0.6"
    assert s.primary_clock.name == "main"      # legacy clock stays first
    assert s.errors == []


def test_clocks_json_only():
    import json
    s = load_settings({"TUBE_CLOCKS_JSON": json.dumps([{"name": "a", "host": "h", "mqtt_id": "m"}])})
    assert [c.name for c in s.clocks] == ["a"]
    assert s.primary_clock.name == "a"


def test_clocks_json_invalid_json_is_an_error():
    s = load_settings(_env(TUBE_CLOCKS_JSON="{not json"))
    assert s.clocks[0].name == "main"           # legacy clock still there
    assert any("TUBE_CLOCKS_JSON" in e for e in s.errors)


def test_clocks_json_not_a_list():
    s = load_settings(_env(TUBE_CLOCKS_JSON='{"name": "x"}'))
    assert any("JSON list" in e for e in s.errors)


def test_clocks_json_bad_entries_skipped_with_errors():
    import json
    s = load_settings(_env(TUBE_CLOCKS_JSON=json.dumps([
        {"name": "ok", "host": "h1", "mqtt_id": "m1"},
        {"name": "Bad Name!", "host": "h2", "mqtt_id": "m2"},   # invalid name
        {"name": "nohost", "mqtt_id": "m3"},                    # no host
        {"name": "nomqtt", "host": "h4"},                       # no mqtt_id
        {"name": "ok", "host": "h5", "mqtt_id": "m5"},           # duplicate
        "not an object",
    ])))
    assert [c.name for c in s.clocks] == ["main", "ok"]
    assert len(s.errors) >= 5
    assert any("'name'" in e and "bad name!" in e for e in s.errors)
    assert any("nohost" in e for e in s.errors)
    assert any("nomqtt" in e for e in s.errors)
    assert any("duplicate" in e for e in s.errors)


def test_clocks_json_duplicate_main_deduped_keeping_legacy():
    import json
    s = load_settings(_env(TUBE_CLOCKS_JSON=json.dumps([{"name": "main", "host": "other", "mqtt_id": "z"}])))
    assert [c.name for c in s.clocks] == ["main"]
    assert s.clocks[0].host == "clock.local"    # the legacy one wins
    assert any("more than once" in e for e in s.errors)


def test_new_env_fields():
    s = load_settings(_env(TUBE_APP_URL="https://ha.example.com",
                           TUBE_FIRMWARE_UPDATES="false",
                           TUBE_FIRMWARE_VARIANT="awtrix"))
    assert s.app_url == "https://ha.example.com"
    assert s.firmware_updates is False
    assert s.firmware_variant == "awtrix"
    s = load_settings(_env(TUBE_FIRMWARE_UPDATES="0"))
    assert s.firmware_updates is False
    s = load_settings(_env(TUBE_FIRMWARE_UPDATES="yes"))
    assert s.firmware_updates is True


def test_settings_built_directly_defaults_to_main_clock():
    # tests (and embedders) build Settings(clock_host=...) directly: that must
    # behave exactly like the legacy config - one clock named "main"
    s = Settings(clock_host="clock.test", clock_mqtt_id="abc")
    assert len(s.clocks) == 1
    assert s.clocks[0] == ClockSettings(name="main", host="clock.test", mqtt_id="abc")
    assert s.primary_clock.name == "main"
    # explicit clocks are kept as given
    s2 = Settings(clock_host="", clock_mqtt_id="",
                  clocks=[ClockSettings("a", "h", "m"), ClockSettings("b", "h2", "m2")])
    assert [c.name for c in s2.clocks] == ["a", "b"]
    assert s2.primary_clock.name == "a"
    # no clock_host and no clocks -> empty
    s3 = Settings(clock_host="", clock_mqtt_id="")
    assert s3.clocks == [] and s3.primary_clock is None


def test_clock_settings_base_url_rules():
    assert ClockSettings("a", "host", "m").base_url == "http://host"
    assert ClockSettings("a", "http://host/", "m").base_url == "http://host"
    assert ClockSettings("a", "https://h:1/", "m").base_url == "https://h:1"
