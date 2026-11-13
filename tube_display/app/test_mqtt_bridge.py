"""MqttBridge: per-clock topics/entities, message routing, publishing.

Uses a fake aiomqtt-like client (publishes recorded) and fake controllers -
no Pillow, no controller/render import, no network.
"""

import asyncio
import json

import pytest

import mqtt_bridge
from mqtt_bridge import MqttBridge


class FakeClock:
    def __init__(self, mqtt_id):
        self.mqtt_id = mqtt_id


class FakeController:
    """Records the callbacks the bridge makes; stands in for controller.Controller."""

    def __init__(self, name, mqtt_id):
        self.name = name
        self.clock = FakeClock(mqtt_id)
        self.calls = []          # (method, args...)
        self.state = {"screen": "Rotate", "screen_options": ["Rotate", "Clock", "a"],
                      "rotation": "on"}
        self._status = {"current": "a", "last_error": None, "ota_busy": False,
                        "message": None, "paused": False, "quiet": False, "online": True}

    def _mqtt_state(self):
        return dict(self.state)

    def status(self):
        return dict(self._status)

    def show_screen(self, name, seconds=None):
        if name not in ("a", "clock"):
            raise KeyError(name)
        self.calls.append(("show_screen", name, seconds))

    def clear_pin(self):
        self.calls.append(("clear_pin",))

    def next_screen(self):
        self.calls.append(("next_screen",))

    def set_rotation_enabled(self, on):
        self.calls.append(("set_rotation_enabled", on))

    def pause(self, seconds=None):
        self.calls.append(("pause", seconds))

    def resume(self):
        self.calls.append(("resume",))

    def flash(self, message, seconds=10, icon=None, color=None):
        self.calls.append(("flash", message, seconds, icon, color))

    async def on_clock_state(self, state):
        self.calls.append(("on_clock_state", state))

    async def on_clock_availability(self, online):
        self.calls.append(("on_clock_availability", online))

    async def on_ota_state(self, state):
        self.calls.append(("on_ota_state", state))


class FakeClient:
    """Records publishes as (topic, payload, retain) and subscriptions."""

    def __init__(self):
        self.published = []
        self.subscribed = []

    async def publish(self, topic, payload, retain=False):
        self.published.append((str(topic), payload, retain))

    async def subscribe(self, topic):
        self.subscribed.append(str(topic))


class FakeMessage:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload.encode() if isinstance(payload, str) else payload


def _bridge(*controllers):
    b = MqttBridge({"host": "broker"}, list(controllers))
    b._client = FakeClient()
    return b


def _run(coro):
    return asyncio.run(coro)


def _pub(bridge, topic):
    return [p for p in bridge._client.published if p[0] == topic]


# ----------------------------------------------------------------------
# discovery: first clock keeps its legacy ids and topics, second clock prefixed
# ----------------------------------------------------------------------

LEGACY_DEVICE = {
    "identifiers": ["tube_display"],
    "name": "Tube Display",
    "manufacturer": "nict41's HA Apps",
    "model": "Tube Display",
}


def test_first_clock_discovery_matches_legacy_select_and_switch():
    ctl = FakeController("main", "abc")
    b = _bridge(ctl)
    _run(b.publish_discovery())
    disc = {p[0]: json.loads(p[1]) for p in b._client.published if p[0].startswith("homeassistant/")}
    select = disc["homeassistant/select/tube_display_screen/config"]
    assert select == {
        "unique_id": "tube_display_screen",
        "object_id": "tube_display_screen",
        "name": "Screen",
        "icon": "mdi:view-dashboard-variant",
        "state_topic": "tube_display/screen/state",
        "command_topic": "tube_display/screen/set",
        "options": ["Rotate", "Clock", "a"],
        "availability_topic": "tube_display/availability",
        "device": LEGACY_DEVICE,
    }
    switch = disc["homeassistant/switch/tube_display_rotation/config"]
    assert switch == {
        "unique_id": "tube_display_rotation",
        "object_id": "tube_display_rotation",
        "name": "Rotation",
        "icon": "mdi:sync",
        "state_topic": "tube_display/rotation/state",
        "command_topic": "tube_display/rotation/set",
        "payload_on": "on",
        "payload_off": "off",
        "availability_topic": "tube_display/availability",
        "device": LEGACY_DEVICE,
    }


def test_first_clock_new_entities():
    ctl = FakeController("main", "abc")
    b = _bridge(ctl)
    _run(b.publish_discovery())
    disc = {p[0]: json.loads(p[1]) for p in b._client.published if p[0].startswith("homeassistant/")}
    current = disc["homeassistant/sensor/tube_display_current/config"]
    assert current["unique_id"] == "tube_display_current"
    assert current["name"] == "Current Screen"
    assert current["icon"] == "mdi:television-play"
    assert current["state_topic"] == "tube_display/current/state"
    assert current["availability_topic"] == "tube_display/availability"
    assert current["device"] == LEGACY_DEVICE
    err = disc["homeassistant/sensor/tube_display_last_error/config"]
    assert err["entity_category"] == "diagnostic"
    assert err["state_topic"] == "tube_display/last_error/state"
    online = disc["homeassistant/binary_sensor/tube_display_clock_online/config"]
    assert online["device_class"] == "connectivity"
    assert online["payload_on"] == "on" and online["payload_off"] == "off"
    pause = disc["homeassistant/switch/tube_display_pause/config"]
    assert pause["icon"] == "mdi:pause-circle"
    assert pause["command_topic"] == "tube_display/pause/set"
    msg = disc["homeassistant/text/tube_display_message/config"]
    assert msg["icon"] == "mdi:message-text" and msg["max"] == 255
    assert msg["command_topic"] == "tube_display/message/set"
    nxt = disc["homeassistant/button/tube_display_next/config"]
    assert nxt["icon"] == "mdi:skip-next" and nxt["payload_press"] == "PRESS"
    assert nxt["command_topic"] == "tube_display/next"
    # all retained
    assert all(p[2] is True for p in b._client.published if p[0].startswith("homeassistant/"))


def test_second_clock_prefixed_entities_and_device():
    c1 = FakeController("main", "abc")
    c2 = FakeController("kitchen", "k1")
    b = _bridge(c1, c2)
    _run(b.publish_discovery())
    disc = {p[0]: json.loads(p[1]) for p in b._client.published if p[0].startswith("homeassistant/")}
    select = disc["homeassistant/select/tube_display_kitchen_screen/config"]
    assert select["unique_id"] == "tube_display_kitchen_screen"
    assert select["name"] == "Screen"
    assert select["state_topic"] == "tube_display/kitchen/screen/state"
    assert select["command_topic"] == "tube_display/kitchen/screen/set"
    assert select["availability_topic"] == "tube_display/availability"
    assert select["device"] == {
        "identifiers": ["tube_display_kitchen"],
        "name": "Tube Display Kitchen",
        "manufacturer": "nict41's HA Apps",
        "model": "Tube Display",
        "via_device": "tube_display",
    }
    assert "homeassistant/sensor/tube_display_kitchen_current/config" in disc
    assert "homeassistant/sensor/tube_display_kitchen_last_error/config" in disc
    assert "homeassistant/binary_sensor/tube_display_kitchen_clock_online/config" in disc
    assert "homeassistant/switch/tube_display_kitchen_pause/config" in disc
    assert "homeassistant/text/tube_display_kitchen_message/config" in disc
    assert "homeassistant/button/tube_display_kitchen_next/config" in disc
    # the first clock's entities are untouched
    assert "homeassistant/select/tube_display_screen/config" in disc
    # ONE availability topic for every clock (also the connection's last will)
    for cfg in disc.values():
        assert cfg["availability_topic"] == "tube_display/availability"


# ----------------------------------------------------------------------
# routing
# ----------------------------------------------------------------------

def test_routing_clock_topics_to_the_right_controller():
    c1 = FakeController("main", "abc")
    c2 = FakeController("kitchen", "k1")
    b = _bridge(c1, c2)
    _run(b._handle(FakeMessage("clock/abc/volatile/state", '{"display": 4}')))
    _run(b._handle(FakeMessage("clock/k1/volatile/state", '{"display": 0}')))
    _run(b._handle(FakeMessage("clock/abc/button", "next")))
    _run(b._handle(FakeMessage("clock/k1/button", "next")))
    _run(b._handle(FakeMessage("clock/abc/availability", "offline")))
    _run(b._handle(FakeMessage("clock/k1/availability", "online")))
    _run(b._handle(FakeMessage("clock/abc/ota/state", '{"in_progress": true}')))
    _run(b._handle(FakeMessage("clock/k1/ota/state", '{"in_progress": false}')))
    assert c1.calls == [("on_clock_state", {"display": 4}), ("next_screen",),
                        ("on_clock_availability", False), ("on_ota_state", {"in_progress": True})]
    assert c2.calls == [("on_clock_state", {"display": 0}), ("next_screen",),
                        ("on_clock_availability", True), ("on_ota_state", {"in_progress": False})]


def test_routing_command_topics():
    c1 = FakeController("main", "abc")
    c2 = FakeController("kitchen", "k1")
    b = _bridge(c1, c2)
    _run(b._handle(FakeMessage("tube_display/screen/set", "a")))
    _run(b._handle(FakeMessage("tube_display/kitchen/screen/set", "clock")))
    _run(b._handle(FakeMessage("tube_display/rotation/set", "off")))
    _run(b._handle(FakeMessage("tube_display/kitchen/rotation/set", "on")))
    _run(b._handle(FakeMessage("tube_display/pause/set", "on")))
    _run(b._handle(FakeMessage("tube_display/kitchen/pause/set", "off")))
    _run(b._handle(FakeMessage("tube_display/message/set", "hello")))
    _run(b._handle(FakeMessage("tube_display/kitchen/message/set",
                               '{"message": "hi", "seconds": 30, "icon": "mdi:bell", "color": "#FF0000"}')))
    _run(b._handle(FakeMessage("tube_display/next", "PRESS")))
    _run(b._handle(FakeMessage("tube_display/kitchen/next", "PRESS")))
    assert c1.calls == [("show_screen", "a", None), ("set_rotation_enabled", False),
                        ("pause", None), ("flash", "hello", 10, None, None), ("next_screen",)]
    assert c2.calls == [("show_screen", "clock", None), ("set_rotation_enabled", True),
                        ("resume",),
                        ("flash", "hi", 30, "mdi:bell", "#FF0000"), ("next_screen",)]


def test_select_rotate_and_clock_and_unknown():
    ctl = FakeController("main", "abc")
    b = _bridge(ctl)
    _run(b._handle(FakeMessage("tube_display/screen/set", "Rotate")))
    _run(b._handle(FakeMessage("tube_display/screen/set", "Clock")))
    _run(b._handle(FakeMessage("tube_display/screen/set", "ghost")))   # logged, not raised
    assert ctl.calls == [("clear_pin",), ("show_screen", "clock", None)]


def test_unknown_topic_ignored():
    ctl = FakeController("main", "abc")
    b = _bridge(ctl)
    _run(b._handle(FakeMessage("something/else", "x")))
    assert ctl.calls == []


# ----------------------------------------------------------------------
# publishing
# ----------------------------------------------------------------------

def test_publish_display_brightness_and_generic():
    c1 = FakeController("main", "abc")
    c2 = FakeController("kitchen", "k1")
    b = _bridge(c1, c2)
    _run(b.publish_display("abc", 4))
    _run(b.publish_display("k1", 0))
    _run(b.publish_brightness("abc", 128))
    _run(b.publish_brightness("k1", 42.7))
    _run(b.publish("some/topic", "payload", retain=False))
    pub = b._client.published
    assert ("clock/abc/set/display", "4", False) in pub
    assert ("clock/k1/set/display", "0", False) in pub
    assert ("clock/abc/set/brightness", "128", False) in pub
    assert ("clock/k1/set/brightness", "42", False) in pub     # str(int(value))
    assert ("some/topic", "payload", False) in pub


def test_publish_state_all_entities_and_json_status():
    ctl = FakeController("main", "abc")
    ctl._status.update({"current": "a", "last_error": "boom", "online": True,
                        "paused": False, "message": None, "ota_busy": False, "quiet": False})
    b = _bridge(ctl)
    _run(b.publish_state(ctl))
    pub = {p[0]: p[1] for p in b._client.published}
    assert pub["tube_display/screen/state"] == "Rotate"
    assert pub["tube_display/rotation/state"] == "on"
    assert pub["tube_display/current/state"] == "a"
    assert pub["tube_display/last_error/state"] == "boom"
    assert pub["tube_display/clock_online/state"] == "on"
    assert pub["tube_display/pause/state"] == "off"
    assert pub["tube_display/message/state"] == ""
    status = json.loads(pub["tube_display/state"])
    assert status["current"] == "a" and status["last_error"] == "boom"
    assert all(p[2] is True for p in b._client.published)      # retained


def test_publish_state_current_sensor_reflects_what_shows():
    ctl = FakeController("main", "abc")
    b = _bridge(ctl)
    cases = [
        ({"ota_busy": True, "current": "a"}, "ota"),
        ({"message": "hi", "current": "a"}, "message"),
        ({"paused": True, "current": "a"}, "paused"),
        ({"quiet": True, "current": "a"}, "quiet"),
        ({"online": False, "current": "a"}, "offline"),
        ({"current": "clock"}, "clock"),
        ({"current": None}, "clock"),
        ({"current": "a"}, "a"),
    ]
    for status_over, expected in cases:
        ctl._status = {"current": "a", "last_error": None, "ota_busy": False, "message": None,
                       "paused": False, "quiet": False, "online": True}
        ctl._status.update(status_over)
        b._client.published.clear()
        _run(b.publish_state(ctl))
        pub = {p[0]: p[1] for p in b._client.published}
        assert pub["tube_display/current/state"] == expected, status_over


def test_publish_state_truncates_long_error():
    ctl = FakeController("main", "abc")
    ctl._status["last_error"] = "x" * 500
    b = _bridge(ctl)
    _run(b.publish_state(ctl))
    pub = {p[0]: p[1] for p in b._client.published}
    assert len(pub["tube_display/last_error/state"]) == 250


def test_publish_state_second_clock_uses_its_topics():
    c1 = FakeController("main", "abc")
    c2 = FakeController("kitchen", "k1")
    b = _bridge(c1, c2)
    _run(b.publish_state(c2))
    pub = {p[0]: p[1] for p in b._client.published}
    assert pub["tube_display/kitchen/screen/state"] == "Rotate"
    assert "tube_display/kitchen/state" in pub
    assert "tube_display/screen/state" not in pub


def test_compat_properties_first_clock():
    c1 = FakeController("main", "abc")
    c2 = FakeController("kitchen", "k1")
    b = _bridge(c1, c2)
    assert b.clock_state_topic == "clock/abc/volatile/state"
    assert b.clock_button_topic == "clock/abc/button"
    assert b.clock_display_command_topic == "clock/abc/set/display"


def test_no_client_publishes_nothing():
    ctl = FakeController("main", "abc")
    b = MqttBridge({"host": "broker"}, [ctl])
    _run(b.publish_display("abc", 4))       # _client is None: no crash, no publish
    _run(b.publish_state(ctl))
    _run(b.publish_discovery())


# ----------------------------------------------------------------------
# message entity validation (same rules as the HTTP API)
# ----------------------------------------------------------------------

def test_message_plain_text_flashes_10s():
    ctl = FakeController("main", "abc")
    b = _bridge(ctl)
    _run(b._handle(FakeMessage("tube_display/message/set", "hello")))
    assert ctl.calls == [("flash", "hello", 10, None, None)]


def test_message_json_valid():
    ctl = FakeController("main", "abc")
    b = _bridge(ctl)
    _run(b._handle(FakeMessage("tube_display/message/set",
                               '{"message": "hi", "seconds": 30, "icon": "mdi:bell", "color": "#FF00aa"}')))
    assert ctl.calls == [("flash", "hi", 30.0, "mdi:bell", "#FF00aa")]


def test_message_json_defaults_seconds_to_10():
    ctl = FakeController("main", "abc")
    b = _bridge(ctl)
    _run(b._handle(FakeMessage("tube_display/message/set", '{"message": "hi"}')))
    assert ctl.calls == [("flash", "hi", 10.0, None, None)]


@pytest.mark.parametrize("payload", [
    '{"message": "hi", "seconds": null}',          # None used to crash (now + None)
    '{"message": "hi", "seconds": "30"}',
    '{"message": "hi", "seconds": true}',
    '{"message": "hi", "seconds": 0}',
    '{"message": "hi", "seconds": -5}',
    '{"message": "hi", "seconds": 3601}',
    '{"message": "hi", "icon": "bell"}',            # must start with mdi:
    '{"message": "hi", "icon": 5}',
    '{"message": "hi", "color": "red"}',
    '{"message": "hi", "color": "#FFF"}',
    '{"message": "hi", "color": "#GGGGGG"}',
    '{"message": "hi", "color": 123}',
    '{"message": ""}',
    '{"seconds": 30}',
    '{not json',
])
def test_message_invalid_ignored(payload):
    ctl = FakeController("main", "abc")
    b = _bridge(ctl)
    _run(b._handle(FakeMessage("tube_display/message/set", payload)))
    assert ctl.calls == []                          # logged, not flashed, no crash


def test_message_seconds_boundary_3600_ok():
    ctl = FakeController("main", "abc")
    b = _bridge(ctl)
    _run(b._handle(FakeMessage("tube_display/message/set", '{"message": "hi", "seconds": 3600}')))
    assert ctl.calls == [("flash", "hi", 3600.0, None, None)]


# ----------------------------------------------------------------------
# availability: one topic for the whole app, also the last will
# ----------------------------------------------------------------------

def test_availability_topic_is_shared_and_will_is_set(monkeypatch):
    """_run() must connect with will=AVAILABILITY_TOPIC and publish "online" once."""
    c1 = FakeController("main", "abc")
    c2 = FakeController("kitchen", "k1")
    b = MqttBridge({"host": "broker"}, [c1, c2])
    seen = {}

    class FakeWill:
        def __init__(self, topic, payload, qos=0, retain=False, properties=None):
            seen["will"] = (topic, payload, retain)

    class FakeCtxClient:
        def __init__(self, *args, **kwargs):
            seen["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            b._stop = True          # one connection, then the loop exits
            return False

        async def publish(self, topic, payload, retain=False):
            seen.setdefault("published", []).append((str(topic), payload, retain))

        async def subscribe(self, topic):
            pass

        @property
        def messages(self):
            class _Msgs:
                def __aiter__(self):
                    return self

                async def __anext__(self):
                    raise StopAsyncIteration
            return _Msgs()

    monkeypatch.setattr(mqtt_bridge.aiomqtt, "Client", FakeCtxClient)
    monkeypatch.setattr(mqtt_bridge.aiomqtt, "Will", FakeWill)
    _run(b._run())
    assert seen["will"] == ("tube_display/availability", "offline", True)
    assert seen["client_kwargs"]["will"] is not None
    # "online" published exactly once, on the shared topic
    online = [p for p in seen["published"] if p[0] == "tube_display/availability" and p[1] == "online"]
    assert len(online) == 1 and online[0][2] is True
    assert not b.connected.is_set()      # cleared when the connection ended


def test_connected_event_set_and_cleared():
    ctl = FakeController("main", "abc")
    b = MqttBridge({"host": "broker"}, [ctl])
    assert not b.connected.is_set()
    b.connected.set()
    assert b.connected.is_set()
    _run(b.stop())
    assert not b.connected.is_set()


# ----------------------------------------------------------------------
# diagnostic sensors: firmware / last draw / data rate (entity_category diagnostic)
# ----------------------------------------------------------------------

def test_discovery_has_the_three_diagnostic_sensors():
    ctl = FakeController("main", "abc")
    b = _bridge(ctl)
    _run(b.publish_discovery())
    disc = {p[0]: json.loads(p[1]) for p in b._client.published if p[0].startswith("homeassistant/")}
    fw = disc["homeassistant/sensor/tube_display_firmware/config"]
    assert fw["unique_id"] == "tube_display_firmware"
    assert fw["name"] == "Firmware"                      # no device prefix (has_entity_name style)
    assert fw["entity_category"] == "diagnostic"
    assert fw["state_topic"] == "tube_display/firmware/state"
    assert fw["availability_topic"] == "tube_display/availability"
    assert fw["device"] == LEGACY_DEVICE
    draw = disc["homeassistant/sensor/tube_display_last_draw/config"]
    assert draw["name"] == "Last draw"
    assert draw["entity_category"] == "diagnostic"
    assert draw["unit_of_measurement"] == "ms"
    assert draw["state_topic"] == "tube_display/last_draw/state"
    rate = disc["homeassistant/sensor/tube_display_data_rate/config"]
    assert rate["name"] == "Data rate"
    assert rate["entity_category"] == "diagnostic"
    assert rate["unit_of_measurement"] == "B/min"
    assert rate["state_class"] == "measurement"
    assert rate["state_topic"] == "tube_display/data_rate/state"
    # second clock: prefixed, same shape
    c2 = FakeController("kitchen", "k1")
    b2 = _bridge(ctl, c2)
    _run(b2.publish_discovery())
    disc2 = {p[0]: json.loads(p[1]) for p in b2._client.published if p[0].startswith("homeassistant/")}
    fw2 = disc2["homeassistant/sensor/tube_display_kitchen_firmware/config"]
    assert fw2["unique_id"] == "tube_display_kitchen_firmware"
    assert fw2["name"] == "Firmware"
    assert fw2["entity_category"] == "diagnostic"
    assert fw2["state_topic"] == "tube_display/kitchen/firmware/state"
    assert "homeassistant/sensor/tube_display_kitchen_last_draw/config" in disc2
    assert "homeassistant/sensor/tube_display_kitchen_data_rate/config" in disc2
    assert disc2["homeassistant/sensor/tube_display_kitchen_data_rate/config"]["state_class"] == "measurement"


def test_publish_state_publishes_diagnostics_on_change_and_at_most_every_30s(monkeypatch):
    ctl = FakeController("main", "abc")
    ctl._status["clock_info"] = {"firmware": "2.5.0", "last_draw_ms": 211,
                                 "bytes_per_minute": 12345, "notices": []}
    b = _bridge(ctl)
    _run(b.publish_state(ctl))
    pub = {p[0]: p[1] for p in b._client.published}
    assert pub["tube_display/firmware/state"] == "2.5.0"
    assert pub["tube_display/last_draw/state"] == "211"
    assert pub["tube_display/data_rate/state"] == "12345"
    assert all(p[2] is True for p in b._client.published
               if p[0].endswith(("firmware/state", "last_draw/state", "data_rate/state")))
    # unchanged values within 30 s: not re-published
    b._client.published.clear()
    _run(b.publish_state(ctl))
    pub = {p[0]: p[1] for p in b._client.published}
    assert "tube_display/firmware/state" not in pub
    # a changed value is published right away
    ctl._status["clock_info"] = {"firmware": "2.5.1", "last_draw_ms": 90,
                                 "bytes_per_minute": 0, "notices": []}
    _run(b.publish_state(ctl))
    pub = {p[0]: p[1] for p in b._client.published}
    assert pub["tube_display/firmware/state"] == "2.5.1"
    assert pub["tube_display/last_draw/state"] == "90"
    # after 30 s the same values are published again (heartbeat)
    import mqtt_bridge as mb
    t = [mb.time.monotonic()]
    monkeypatch.setattr(mb.time, "monotonic", lambda: t[0])
    b._client.published.clear()
    t[0] += 31.0
    _run(b.publish_state(ctl))
    pub = {p[0]: p[1] for p in b._client.published}
    assert pub["tube_display/firmware/state"] == "2.5.1"
    # unknown values still publish something sane
    ctl._status["clock_info"] = {"firmware": None, "last_draw_ms": None, "bytes_per_minute": 0}
    b._client.published.clear()
    t[0] += 31.0
    _run(b.publish_state(ctl))
    pub = {p[0]: p[1] for p in b._client.published}
    assert pub["tube_display/firmware/state"] == "unknown"
    assert pub["tube_display/last_draw/state"] == "unknown"
    assert pub["tube_display/data_rate/state"] == "0"


def test_publish_state_without_clock_info_publishes_unknown_once():
    ctl = FakeController("main", "abc")     # no clock_info key at all
    b = _bridge(ctl)
    _run(b.publish_state(ctl))
    pub = {p[0]: p[1] for p in b._client.published}
    assert pub["tube_display/firmware/state"] == "unknown"
    assert pub["tube_display/data_rate/state"] == "0"
