"""Rotation order, `when` skipping, "clock" entries, /api/show interrupt/resume.

A fake clock (time advances when the controller sleeps) and a fake template
renderer drive the controller - no network, no real clock.
"""

import asyncio

import pytest
import yaml

from conftest import pil, requires_pil

pytestmark = requires_pil

if pil:
    from controller import Controller
    from rgb565 import TUBE_COUNT
    from screens import CLOCK_ENTRY
    from settings import Settings


class FakeTime:
    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    async def sleep(self, s):
        self.t += s


class FakePusher:
    def __init__(self):
        self.sent = []          # (tube_bytes, needed_mask)
        self.last_error = None
        self.reachable = True
        self.probes = 0
        self.resets = 0

    async def send_tubes(self, tube_bytes, needed_mask=0):
        self.sent.append((tube_bytes, needed_mask))
        return list(range(TUBE_COUNT))

    def reset(self):
        self.resets += 1

    async def probe(self):
        self.probes += 1
        self.reachable = True
        return True


class FakeMqtt:
    def __init__(self):
        self.display_modes = []     # modes, in order (legacy assertions compare these)
        self.display_calls = []     # (mqtt_id, mode)
        self.states = []            # (controller, state dict)
        self.discovery_calls = 0
        self.brightness = []        # (mqtt_id, value)

    async def publish_display(self, mqtt_id, mode):
        self.display_modes.append(mode)
        self.display_calls.append((mqtt_id, mode))

    async def publish_state(self, controller):
        self.states.append((controller, controller._mqtt_state()))

    async def publish_brightness(self, mqtt_id, value):
        self.brightness.append((mqtt_id, value))

    async def publish_discovery(self):
        self.discovery_calls += 1


def _write_screens(path, data):
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f)


def _base_screens():
    return {
        "rotation": ["a", "b", "clock"],
        "clock": {"duration": 60},
        "screens": {
            "a": {"duration": 10, "refresh": 5,
                  "tubes": [{"type": "text", "text": "A"}] * 6},
            "b": {"duration": 10, "refresh": 5,
                  "when": "{{ flag }}",
                  "tubes": [{"type": "text", "text": "B"}] * 6},
        },
    }


def _make_controller(tmp_path, screens_data, renderer=None, clock_state=None):
    path = tmp_path / "screens.yaml"
    _write_screens(path, screens_data)
    settings = Settings(clock_host="clock.test", clock_mqtt_id="abc",
                        screens_file=str(path), default_duration=15, default_refresh=30)
    time = FakeTime()
    pusher = FakePusher()
    mqtt = FakeMqtt()

    async def default_renderer(t):
        return "true"

    controller = Controller(settings, session=None, ha=None, pusher=pusher, mqtt=mqtt,
                            renderer=renderer or default_renderer,
                            now=time.now, sleep=time.sleep)
    controller.clock_state = clock_state or {}
    controller.load_config(force=True)
    return controller, time, pusher, mqtt


def test_rotation_order_and_clock_entry(tmp_path):
    controller, time, pusher, mqtt = _make_controller(tmp_path, _base_screens())
    seen = []
    for _ in range(3):
        entry = asyncio.run(controller.run_next_entry())
        seen.append(entry)
    assert seen == ["a", "b", "clock"]
    # "clock" entry published display=0 and did not send tubes
    assert mqtt.display_modes[-1] == 0
    assert controller.current == CLOCK_ENTRY
    # screen "a" published display=4 first
    assert mqtt.display_modes[0] == 4


def test_when_false_skips_screen(tmp_path):
    async def renderer(t):
        return "false" if t == "{{ flag }}" else "true"

    controller, time, pusher, mqtt = _make_controller(tmp_path, _base_screens(), renderer=renderer)
    seen = [asyncio.run(controller.run_next_entry()) for _ in range(3)]
    assert seen == ["a", "clock", "a"]     # b always skipped
    assert all(s != "b" for s in seen)


def test_durations_honoured(tmp_path):
    data = _base_screens()
    data["rotation"] = ["a"]
    controller, time, pusher, mqtt = _make_controller(tmp_path, data)
    t0 = time.t
    asyncio.run(controller.run_next_entry())
    assert time.t - t0 == pytest.approx(10)   # a.duration = 10


def test_refresh_rerenders_within_screen(tmp_path):
    data = _base_screens()
    data["rotation"] = ["a"]
    data["screens"]["a"]["refresh"] = 4
    data["screens"]["a"]["duration"] = 10
    controller, time, pusher, mqtt = _make_controller(tmp_path, data)
    asyncio.run(controller.run_next_entry())
    # activation sends once (mask=all), refreshes at t+4 and t+8 -> 3 sends
    assert len(pusher.sent) == 3
    assert pusher.sent[0][1] == 0b111111


def test_show_interrupt_and_resume(tmp_path):
    controller, time, pusher, mqtt = _make_controller(tmp_path, _base_screens())
    asyncio.run(controller.run_next_entry())          # shows "a"
    assert controller.current == "a"
    controller.show_screen("b", seconds=30)           # interrupt
    assert controller._pin == ("b", time.t + 30)
    # run the loop body once: pinned screen is shown
    async def one_iteration():
        await controller._reload_if_changed()
        entry, expiry = controller._pin
        duration = expiry - controller._now()
        controller._interrupted = False
        screen = controller._config.screens[entry]
        await controller._show_screen(screen, duration)
    asyncio.run(one_iteration())
    assert controller.current == "b"
    # expire the pin
    time.t += 31
    assert controller._pin is not None
    async def check_expiry():
        await controller._reload_if_changed()
        entry, expiry = controller._pin
        if expiry is not None and controller._now() >= expiry:
            controller._pin = None
    asyncio.run(check_expiry())
    assert controller._pin is None                    # resumed rotation


def test_show_clock_entry(tmp_path):
    controller, time, pusher, mqtt = _make_controller(tmp_path, _base_screens())
    controller.show_screen("clock", seconds=60)
    async def one_iteration():
        await controller._reload_if_changed()
        entry, expiry = controller._pin
        await controller._show_clock(expiry - controller._now())
    asyncio.run(one_iteration())
    assert controller.current == CLOCK_ENTRY
    assert mqtt.display_modes[-1] == 0


def test_show_unknown_screen_raises(tmp_path):
    controller, *_ = _make_controller(tmp_path, _base_screens())
    with pytest.raises(KeyError):
        controller.show_screen("nope")


def test_rotation_pause(tmp_path):
    controller, time, pusher, mqtt = _make_controller(tmp_path, _base_screens())
    controller.set_rotation_enabled(False)
    assert controller.rotation_enabled is False
    assert controller.status()["rotation_enabled"] is False
    controller.set_rotation_enabled(True)
    assert controller.status()["rotation_enabled"] is True


def test_needed_mask_triggers_immediate_resend(tmp_path):
    controller, time, pusher, mqtt = _make_controller(tmp_path, _base_screens())
    asyncio.run(controller.run_next_entry())          # shows "a", sends all
    n = len(pusher.sent)
    asyncio.run(controller.on_clock_state({"display": 4, "panels_needed": 0b000101}))
    assert len(pusher.sent) == n + 1
    assert pusher.sent[-1][1] == 0b000101


def test_display_4_published_only_when_not_already_4(tmp_path):
    controller, time, pusher, mqtt = _make_controller(
        tmp_path, _base_screens(), clock_state={"display": 4})
    asyncio.run(controller.run_next_entry())
    assert 4 not in mqtt.display_modes               # clock already in panels mode


def test_screens_reload_on_mtime_change(tmp_path):
    controller, time, pusher, mqtt = _make_controller(tmp_path, _base_screens())
    assert "a" in controller._config.screens
    data = _base_screens()
    data["screens"]["c"] = {"tubes": [{"type": "blank"}] * 6}
    data["rotation"] = ["a", "c"]
    _write_screens(tmp_path / "screens.yaml", data)
    import os
    os.utime(tmp_path / "screens.yaml", (time.t + 5, time.t + 5))
    asyncio.run(controller._reload_if_changed())
    assert "c" in controller._config.screens
    assert mqtt.discovery_calls >= 1                  # discovery republished


def test_status_shape(tmp_path):
    controller, time, pusher, mqtt = _make_controller(tmp_path, _base_screens())
    asyncio.run(controller.run_next_entry())
    status = controller.status()
    assert status["current"] == "a"
    assert status["rotation"] == ["a", "b", "clock"]
    assert status["screens"] == ["a", "b"]
    assert status["clock_host"] == "clock.test"
    assert "clock_state" in status


def test_rotation_off_button_steps_screens_and_skips_clock(tmp_path, monkeypatch):
    from controller import Controller
    monkeypatch.setattr(Controller, "STATE_FILE", str(tmp_path / "state.json"))
    controller, time, pusher, mqtt = _make_controller(tmp_path, _base_screens())
    controller.load_config(force=True)
    controller.set_rotation_enabled(False)
    shown = []
    for _ in range(4):
        controller.next_screen()
        assert controller._advance is True
        controller._advance = False
        asyncio.run(controller._manual_step())
        shown.append(controller.current)
    assert shown[0] == "a" and set(shown) <= {"a", "b"}   # the clock entry is never chosen
    # remembered across a restart
    controller2, *_ = _make_controller(tmp_path, _base_screens())
    assert controller2.rotation_enabled is False
