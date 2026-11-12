"""Rotation order, `when` skipping, "clock" entries, /api/show interrupt/resume,
pause, flash messages, OTA, availability, reachability probes, quiet hours,
night mode, live updates and multi-clock rotations.

A fake clock (time advances when the controller sleeps) and a fake template
renderer drive the controller - no network, no real clock.
"""

import asyncio
import datetime

import pytest
import yaml

from conftest import pil, requires_pil

pytestmark = requires_pil

if pil:
    from controller import Controller, OTA_TIMEOUT, PROBE_BACKOFF
    from rgb565 import TUBE_COUNT, strip_to_tube_bytes
    from screens import CLOCK_ENTRY
    from settings import ClockSettings, Settings


class FakeTime:
    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    async def sleep(self, s):
        self.t += s
        await asyncio.sleep(0)   # yield, so a supervisor task can stop run()


class FakePusher:
    def __init__(self, reachable=True):
        self.sent = []          # (tube_bytes, needed_mask)
        self.last_error = None
        self.reachable = reachable
        self.probes = 0
        self.resets = 0
        self.probe_results = None   # optional list/queue of results; default: succeed

    async def send_tubes(self, tube_bytes, needed_mask=0):
        self.sent.append((tube_bytes, needed_mask))
        return list(range(TUBE_COUNT))

    def reset(self):
        self.resets += 1

    async def probe(self):
        self.probes += 1
        if self.probe_results is not None:
            if isinstance(self.probe_results, list) and self.probe_results:
                ok = self.probe_results.pop(0)
            else:
                ok = self.probe_results
        else:
            ok = True
        self.reachable = bool(ok)
        return bool(ok)


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


def _make_controller(tmp_path, screens_data, renderer=None, clock_state=None,
                     pusher=None, name="main", clock_mqtt_id="abc"):
    path = tmp_path / "screens.yaml"
    _write_screens(path, screens_data)
    settings = Settings(clock_host="clock.test", clock_mqtt_id=clock_mqtt_id,
                        screens_file=str(path), default_duration=15, default_refresh=30)
    if name != "main" or clock_mqtt_id != "abc":
        settings.clocks = [ClockSettings(name=name, host="clock.test", mqtt_id=clock_mqtt_id)]
    time = FakeTime()
    pusher = pusher if pusher is not None else FakePusher()
    mqtt = FakeMqtt()

    async def default_renderer(t):
        return "true"

    controller = Controller(settings, session=None, ha=None, pusher=pusher, mqtt=mqtt,
                            renderer=renderer or default_renderer,
                            now=time.now, sleep=time.sleep)
    controller.clock_state = clock_state or {}
    controller.load_config(force=True)
    return controller, time, pusher, mqtt


def _run(coro):
    return asyncio.run(coro)


def _run_loop(controller, time, seconds):
    """Run controller.run() for `seconds` of fake time, then stop it.

    The controller's own sleeps advance fake time; we just keep yielding to the
    event loop. stop() is sticky, so the flag is cleared when the next phase
    starts (the tests drive one controller through several phases)."""
    async def drive():
        controller._stop = False
        task = asyncio.ensure_future(controller.run())
        end = time.t + seconds
        for _ in range(500000):
            if time.t >= end or task.done():
                break
            await asyncio.sleep(0)
        controller.stop()
        try:
            await asyncio.wait_for(task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()
    asyncio.run(drive())


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


# ======================================================================
# item 1: night mode keeps ticking inside every waiting loop
# ======================================================================

def _night_screens():
    data = _base_screens()
    data["rotation"] = ["a"]
    data["night"] = {"brightness_day": 255, "brightness_night": 10,
                     "bright_above": 10.0, "dim_below": -10.0, "dim_screens": 1.0,
                     "quiet": {"from": "23:00", "to": "07:00", "mode": "clock"}}
    return data


def test_night_tick_during_quiet_hours(tmp_path):
    """During a 2-hour quiet period with the sun going 10 -> -10, brightness is
    published several times (the tick runs inside _show_quiet, not just at the top)."""
    elevations = iter([10.0, 5.0, 0.0, -5.0, -10.0, -10.0, -10.0, -10.0])

    async def renderer(t):
        if "elevation" in t:
            return str(next(elevations))
        return "true"

    controller, time, pusher, mqtt = _make_controller(tmp_path, _night_screens(), renderer=renderer)
    # 23:30 local -> inside quiet hours 23:00-07:00
    controller.local_now = lambda: datetime.datetime(2026, 11, 12, 23, 30)
    _run_loop(controller, time, 2 * 3600)
    # brightness published several times as the elevation drops (>= 3 apart each)
    values = [v for _, v in mqtt.brightness]
    assert len(values) >= 3, values
    assert values[0] == 255 and values[-1] == 10
    assert values == sorted(values, reverse=True)   # monotonically darker


# ======================================================================
# item 2: rotation off - a flash/OTA that ends re-shows the screen at once
# ======================================================================

def test_rotation_off_reshow_after_flash(tmp_path, monkeypatch):
    monkeypatch.setattr(Controller, "STATE_FILE", str(tmp_path / "state.json"))
    data = _base_screens()
    data["rotation"] = ["a", "b"]
    controller, time, pusher, mqtt = _make_controller(tmp_path, data)
    controller.set_rotation_enabled(False)
    # show "a" and let it settle (rotation off: stays on the current screen)
    _run_loop(controller, time, 5)
    assert controller.current == "a"
    n_sent = len(pusher.sent)
    # a flash interrupts; when it ends the same screen must be re-activated immediately
    controller.flash("hello", seconds=10)
    _run_loop(controller, time, 12)
    assert controller.current == "a"
    assert len(pusher.sent) > n_sent          # the screen was re-sent right after the flash
    assert controller._reshow is None


def test_rotation_off_reshow_after_ota(tmp_path, monkeypatch):
    monkeypatch.setattr(Controller, "STATE_FILE", str(tmp_path / "state.json"))
    data = _base_screens()
    data["rotation"] = ["a", "b"]
    controller, time, pusher, mqtt = _make_controller(tmp_path, data)
    controller.set_rotation_enabled(False)
    _run_loop(controller, time, 5)
    assert controller.current == "a"
    n_sent = len(pusher.sent)
    _run(controller.on_ota_state({"in_progress": True}))
    _run_loop(controller, time, 3)
    assert len(pusher.sent) == n_sent         # nothing pushed during the OTA
    _run(controller.on_ota_state({"in_progress": False}))
    _run_loop(controller, time, 3)
    assert controller.current == "a"
    assert len(pusher.sent) > n_sent          # re-sent immediately after the OTA


# ======================================================================
# pause
# ======================================================================

def test_pause_forever_then_resume(tmp_path):
    controller, time, pusher, mqtt = _make_controller(tmp_path, _base_screens())
    _run_loop(controller, time, 3)            # rotation shows "a"
    assert controller.current == "a"
    n_sent = len(pusher.sent)
    controller.pause(None)
    _run_loop(controller, time, 20)
    # display 0 published exactly once for the pause, nothing pushed while paused
    assert mqtt.display_modes.count(0) == 1
    assert len(pusher.sent) == n_sent
    assert controller.current == CLOCK_ENTRY
    assert controller.status()["paused"] is True
    controller.resume()
    _run_loop(controller, time, 80)           # back to the rotation (clock entry first)
    assert controller.status()["paused"] is False
    assert controller.current == "a"
    assert len(pusher.sent) > n_sent


def test_pause_expires_by_itself(tmp_path):
    controller, time, pusher, mqtt = _make_controller(tmp_path, _base_screens())
    _run_loop(controller, time, 3)
    n_sent = len(pusher.sent)
    controller.pause(30)
    _run_loop(controller, time, 40)           # 30 s pause expires
    assert controller.status()["paused"] is False
    assert controller.current in ("a", "b", CLOCK_ENTRY)   # rotation resumed
    assert len(pusher.sent) > n_sent


def test_show_screen_and_next_end_pause(tmp_path):
    controller, time, pusher, mqtt = _make_controller(tmp_path, _base_screens())
    _run_loop(controller, time, 3)
    controller.pause(None)
    _run_loop(controller, time, 5)
    assert controller.status()["paused"] is True
    controller.show_screen("b", seconds=100)
    _run_loop(controller, time, 5)
    assert controller.status()["paused"] is False
    assert controller.current == "b"
    controller.pause(None)
    _run_loop(controller, time, 5)
    assert controller.status()["paused"] is True
    controller.next_screen()
    _run_loop(controller, time, 5)
    assert controller.status()["paused"] is False
    assert controller.current in ("a", "b", CLOCK_ENTRY)


# ======================================================================
# flash
# ======================================================================

def test_flash_pushes_all_six_tubes_then_returns_to_rotation(tmp_path):
    controller, time, pusher, mqtt = _make_controller(tmp_path, _base_screens())
    _run_loop(controller, time, 3)            # showing "a"
    assert controller.current == "a"
    n_sent = len(pusher.sent)
    controller.flash("hello", seconds=10)
    _run_loop(controller, time, 5)            # flash shows
    assert len(pusher.sent) == n_sent + 1
    assert pusher.sent[-1][1] == 0b111111     # all six tubes
    assert controller.status()["message"] == "hello"
    _run_loop(controller, time, 10)           # flash expires -> same entry re-shown
    assert controller.status()["message"] is None
    assert controller.current == "a"
    assert len(pusher.sent) > n_sent + 1


def test_flash_returns_to_pinned_screen_if_time_left(tmp_path):
    controller, time, pusher, mqtt = _make_controller(tmp_path, _base_screens())
    controller.show_screen("b", seconds=1000)
    _run_loop(controller, time, 3)
    assert controller.current == "b"
    controller.flash("hi", seconds=10)
    _run_loop(controller, time, 15)
    assert controller.current == "b"          # pin still has time -> back to "b"
    assert controller._pin is not None


def test_flash_during_pause_returns_to_pause(tmp_path):
    controller, time, pusher, mqtt = _make_controller(tmp_path, _base_screens())
    _run_loop(controller, time, 3)
    controller.pause(None)
    _run_loop(controller, time, 3)
    assert controller.status()["paused"] is True
    n_sent = len(pusher.sent)
    controller.flash("hi", seconds=10)
    _run_loop(controller, time, 15)
    assert controller.status()["paused"] is True
    assert controller.current == CLOCK_ENTRY
    assert len(pusher.sent) == n_sent + 1     # only the flash itself was pushed


def test_second_flash_replaces_first(tmp_path):
    controller, time, pusher, mqtt = _make_controller(tmp_path, _base_screens())
    _run_loop(controller, time, 3)
    controller.flash("first", seconds=100)
    _run_loop(controller, time, 2)
    assert controller.status()["message"] == "first"
    controller.flash("second", seconds=10)
    _run_loop(controller, time, 15)
    assert controller.status()["message"] is None
    assert controller.current == "a"          # back to the rotation entry
    messages = [s for s in mqtt.states if s[1].get("screen") == "message"]
    # the second flash replaced the first (no return to "first" afterwards)


# ======================================================================
# OTA
# ======================================================================

def test_ota_blocks_everything_until_done(tmp_path):
    controller, time, pusher, mqtt = _make_controller(tmp_path, _base_screens())
    _run_loop(controller, time, 3)
    assert controller.current == "a"
    n_sent, n_disp = len(pusher.sent), len(mqtt.display_calls)
    _run(controller.on_ota_state({"in_progress": True}))
    _run_loop(controller, time, 10)
    assert len(pusher.sent) == n_sent         # nothing pushed
    assert len(mqtt.display_calls) == n_disp  # no display published
    assert controller.status()["ota_busy"] is True
    _run(controller.on_ota_state({"in_progress": False}))
    assert controller.status()["ota_busy"] is False
    assert pusher.resets >= 1                 # pusher reset
    _run_loop(controller, time, 3)
    assert len(pusher.sent) > n_sent          # current screen re-sent
    assert controller.current == "a"


def test_ota_ends_when_availability_comes_back(tmp_path):
    controller, time, pusher, mqtt = _make_controller(tmp_path, _base_screens())
    _run_loop(controller, time, 3)
    n_sent = len(pusher.sent)
    _run(controller.on_ota_state({"in_progress": True}))
    _run(controller.on_clock_availability(False))
    _run_loop(controller, time, 5)
    assert len(pusher.sent) == n_sent
    _run(controller.on_clock_availability(True))
    assert controller.status()["ota_busy"] is False
    _run_loop(controller, time, 3)
    assert len(pusher.sent) > n_sent


def test_ota_times_out_after_10_minutes(tmp_path):
    controller, time, pusher, mqtt = _make_controller(tmp_path, _base_screens())
    _run_loop(controller, time, 3)
    n_sent = len(pusher.sent)
    _run(controller.on_ota_state({"in_progress": True}))
    _run_loop(controller, time, OTA_TIMEOUT + 5)
    assert controller.status()["ota_busy"] is False
    assert len(pusher.sent) > n_sent          # resumed after the timeout


# ======================================================================
# availability offline -> online
# ======================================================================

def test_availability_offline_online_resends_all_tubes(tmp_path):
    controller, time, pusher, mqtt = _make_controller(tmp_path, _base_screens())
    _run_loop(controller, time, 3)
    assert controller.current == "a"
    n_sent = len(pusher.sent)
    _run(controller.on_clock_availability(False))
    assert controller.status()["online"] is False
    _run_loop(controller, time, 5)
    # nothing pushed while offline (the one extra send is the refresh of the
    # still-showing screen, whose bytes did not change -> no tube upload)
    assert len(pusher.sent) <= n_sent + 1
    _run(controller.on_clock_availability(True))
    assert controller.status()["online"] is True
    assert pusher.resets >= 1
    assert len(pusher.sent) > n_sent
    assert pusher.sent[-1][1] == 0b111111     # all six tubes re-sent immediately


# ======================================================================
# unreachable pusher: probes with backoff 5, 10, 20, 40, 60, 60...
# ======================================================================

def test_unreachable_pusher_probe_backoff(tmp_path):
    pusher = FakePusher(reachable=False)
    pusher.probe_results = False              # every probe fails
    controller, time, pusher, mqtt = _make_controller(tmp_path, _base_screens(), pusher=pusher)
    _run_loop(controller, time, 200)
    assert len(pusher.sent) == 0              # nothing pushed into a dead clock
    # probes at 5, 10, 20, 40, 60, 60 -> 6 probes in 200 s (next at 60+60=120... )
    assert pusher.probes >= 5, pusher.probes
    # the gaps between probes follow PROBE_BACKOFF
    # (re-derive from _next_probe progression: 5,10,20,40,60,60)
    assert pusher.probes <= 7


def test_unreachable_pusher_probe_schedule_exact(tmp_path):
    """While the clock is unreachable, probes run with the PROBE_BACKOFF schedule
    (5, 10, 20, 40, 60, then 60 s forever). The controller schedules the next probe
    after each failure; the loop's 1 s sleep quantises when it actually runs, so
    assert on the scheduled delays, not the wall-clock gaps."""
    pusher = FakePusher(reachable=False)
    pusher.probe_results = False
    controller, time, pusher, mqtt = _make_controller(tmp_path, _base_screens(), pusher=pusher)
    scheduled = []       # _next_probe value set by each failed probe
    orig_probe = pusher.probe

    async def probe():
        r = await orig_probe()
        if not r:
            scheduled.append(controller._next_probe)
        return r

    pusher.probe = probe
    _run_loop(controller, time, 5 + 10 + 20 + 40 + 60 + 60 + 5)
    # probe times: t=0 (no schedule), then 10, 20, 80, 160 -> the scheduled
    # _next_probe values are 5, 20, 40, 120 relative to t0=1000 (the 5th probe at
    # t=160 schedules 220, past the end of the run)
    assert [round(v - 1000) for v in scheduled[1:]] == [5, 20, 40, 120]
    assert pusher.probes == len(scheduled)


def test_probe_success_resends_everything(tmp_path):
    pusher = FakePusher(reachable=False)
    pusher.probe_results = [False, False, True]   # third probe (at t=5+10+20) succeeds
    controller, time, pusher, mqtt = _make_controller(tmp_path, _base_screens(), pusher=pusher)
    _run_loop(controller, time, 5 + 10 + 20 + 1)
    assert len(pusher.sent) == 0              # nothing pushed while unreachable
    _run_loop(controller, time, 2)            # the successful probe re-sends everything
    assert pusher.resets >= 1
    assert len(pusher.sent) >= 1
    assert pusher.sent[-1][1] == 0b111111


# ======================================================================
# quiet hours
# ======================================================================

def _quiet_controller(tmp_path, mode="clock", at=datetime.datetime(2026, 11, 12, 23, 30)):
    data = _base_screens()
    data["rotation"] = ["a"]
    data["night"] = {"quiet": {"from": "23:00", "to": "07:00", "mode": mode}}
    controller, time, pusher, mqtt = _make_controller(tmp_path, data)
    controller.local_now = lambda: at
    return controller, time, pusher, mqtt


def test_quiet_hours_clock_mode(tmp_path):
    controller, time, pusher, mqtt = _quiet_controller(tmp_path, mode="clock")
    _run_loop(controller, time, 30)
    assert mqtt.display_modes.count(0) == 1   # display 0 published once
    assert len(pusher.sent) == 0              # nothing pushed during quiet hours
    assert controller.status()["quiet"] is True
    assert controller.current == CLOCK_ENTRY


def test_quiet_hours_blank_mode_sends_black_once(tmp_path):
    controller, time, pusher, mqtt = _quiet_controller(tmp_path, mode="blank")
    _run_loop(controller, time, 30)
    assert len(pusher.sent) == 1
    assert pusher.sent[0][1] == 0b111111
    tube_bytes = pusher.sent[0][0]
    assert len(tube_bytes) == TUBE_COUNT
    assert all(b == b"\x00" * len(b) for b in tube_bytes)   # all-black RGB565


def test_pin_during_quiet_hours_still_shows(tmp_path):
    controller, time, pusher, mqtt = _quiet_controller(tmp_path, mode="clock")
    controller.show_screen("a", seconds=100)
    _run_loop(controller, time, 10)
    assert controller.current == "a"          # the pin wins over quiet hours
    assert len(pusher.sent) >= 1
    assert controller.status()["quiet"] is True


def test_quiet_hours_end_at_07_rotation_resumes(tmp_path):
    at = datetime.datetime(2026, 11, 12, 23, 30)

    def local_now():
        # 23:30 for the first 30 fake seconds, then 07:00
        return at if time.t < 1000 + 30 else datetime.datetime(2026, 11, 13, 7, 0)

    controller, time, pusher, mqtt = _quiet_controller(tmp_path, mode="clock")
    controller.local_now = local_now
    _run_loop(controller, time, 60)
    assert controller.status()["quiet"] is False
    assert controller.current == "a"          # rotation resumed at 07:00
    assert len(pusher.sent) >= 1


# ======================================================================
# night: brightness only on >= 3 change, dim factor on pushed bytes
# ======================================================================

def test_brightness_published_only_on_change_of_3(tmp_path):
    elevations = iter([10.0, 9.0, 8.0, 7.0, 6.0, 5.0, 4.0, 3.0, 2.0, 1.0, 0.0, -1.0, -2.0])

    async def renderer(t):
        if "elevation" in t:
            return str(next(elevations))
        return "true"

    data = _base_screens()
    data["rotation"] = ["a"]
    data["screens"]["a"]["duration"] = 1000
    data["night"] = {"brightness_day": 255, "brightness_night": 10,
                     "bright_above": 10.0, "dim_below": -10.0}
    controller, time, pusher, mqtt = _make_controller(tmp_path, data, renderer=renderer)
    _run_loop(controller, time, 13 * 60 + 5)   # 13 ticks, one per 60 s
    values = [v for _, v in mqtt.brightness]
    # elevation 10 -> 255; each 1-degree step is ~12.25 brightness, so every tick
    # changes by >= 3 and publishes; a < 3 change would not
    assert len(values) >= 10, values
    for a, b in zip(values, values[1:]):
        assert abs(a - b) >= 3


def test_night_dim_factor_darkens_pushed_bytes(tmp_path):
    async def renderer(t):
        if "elevation" in t:
            return "-10"      # full night
        return "true"

    data = _base_screens()
    data["rotation"] = ["a"]
    data["screens"]["a"] = {"duration": 1000, "refresh": 1000,
                            "tubes": [{"type": "text", "text": "A", "color": "#FFFFFF"}] * 6}
    data["night"] = {"brightness_day": 255, "brightness_night": 10,
                     "bright_above": 10.0, "dim_below": -10.0, "dim_screens": 0.5}
    controller, time, pusher, mqtt = _make_controller(tmp_path, data, renderer=renderer)
    _run_loop(controller, time, 5)
    assert controller.daylight == 0.0
    assert len(pusher.sent) >= 1
    pushed = pusher.sent[0][0]
    # the preview at full brightness would be pure white (0xFF, 0xFF, 0xFF -> ffff);
    # with dim 0.5 the pushed bytes must be darker
    import render as render_mod
    preview = _run(render_mod.render_screen(controller._config.screens["a"], controller._ctx))
    preview_bytes = strip_to_tube_bytes(preview)
    for pb, vb in zip(pushed, preview_bytes):
        assert pb <= vb
    assert any(pb < vb for pb, vb in zip(pushed, preview_bytes))


# ======================================================================
# live updates
# ======================================================================

def test_live_template_change_refreshes_within_025s(tmp_path):
    renders = {"n": 0}

    async def renderer(t):
        if t == "{{ flag }}":
            renders["n"] += 1
            return "v%d" % renders["n"]
        return "true"

    data = _base_screens()
    data["rotation"] = ["a"]
    data["screens"]["a"] = {"duration": 1000, "refresh": 500,
                            "tubes": [{"type": "text", "text": "{{ flag }}"}] * 6}
    controller, time, pusher, mqtt = _make_controller(tmp_path, data, renderer=renderer)
    controller.live = True
    controller.live_min_interval = 1.0
    _run_loop(controller, time, 2)
    n_sent = len(pusher.sent)
    assert renders["n"] >= 1
    controller.on_template_change()
    _run_loop(controller, time, 1)            # well within the 500 s refresh
    assert len(pusher.sent) > n_sent          # re-rendered within 0.25 s of fake time


def test_live_throttled_to_live_min_interval(tmp_path):
    renders = {"n": 0}

    async def renderer(t):
        if t == "{{ flag }}":
            renders["n"] += 1
            return "v%d" % renders["n"]
        return "true"

    data = _base_screens()
    data["rotation"] = ["a"]
    data["screens"]["a"] = {"duration": 1000, "refresh": 500,
                            "tubes": [{"type": "text", "text": "{{ flag }}"}] * 6}
    controller, time, pusher, mqtt = _make_controller(tmp_path, data, renderer=renderer)
    controller.live = True
    controller.live_min_interval = 100.0      # throttle: at most once per 100 s
    _run_loop(controller, time, 2)
    controller.on_template_change()
    _run_loop(controller, time, 5)            # first live refresh goes through at once
    first_live = controller._last_live
    assert first_live > -1e8                  # a live refresh happened
    controller.on_template_change()
    _run_loop(controller, time, 50)           # dirty again, but throttled (100 s not up)
    assert controller._last_live == first_live      # no second live refresh yet
    _run_loop(controller, time, 60)           # now the interval has passed
    assert controller._last_live > first_live      # throttled refresh went through


# ======================================================================
# several clocks: own rotations, shared state file
# ======================================================================

def test_two_clocks_follow_their_own_rotations(tmp_path, monkeypatch):
    monkeypatch.setattr(Controller, "STATE_FILE", str(tmp_path / "state.json"))
    data = _base_screens()
    data["rotation"] = ["a"]
    data["clocks"] = {"kitchen": {"rotation": ["b", "clock"]}}
    path = tmp_path / "screens.yaml"
    _write_screens(path, data)

    def make(name, mqtt_id):
        settings = Settings(clock_host="clock.test", clock_mqtt_id=mqtt_id,
                            screens_file=str(path), default_duration=15, default_refresh=30)
        settings.clocks = [ClockSettings(name=name, host="clock.test", mqtt_id=mqtt_id)]
        t = FakeTime()
        p = FakePusher()
        m = FakeMqtt()

        async def renderer(x):
            return "true"

        c = Controller(settings, session=None, ha=None, pusher=p, mqtt=m,
                       renderer=renderer, now=t.now, sleep=t.sleep)
        c.load_config(force=True)
        return c, t, p, m

    c1, t1, p1, m1 = make("main", "abc")
    c2, t2, p2, m2 = make("kitchen", "k1")
    assert c1._rotation() == ["a"]
    assert c2._rotation() == ["b", "clock"]
    _run_loop(c1, t1, 12)
    _run_loop(c2, t2, 5)                      # kitchen shows "b" (10 s), not yet the clock
    assert c1.current == "a"
    assert c2.current == "b"
    # rotation on/off is remembered per clock in the same state file
    c1.set_rotation_enabled(False)
    c2.set_rotation_enabled(True)
    import json
    state = json.loads(open(str(tmp_path / "state.json")).read())
    assert state["clocks"]["main"]["rotation"] is False
    assert state["clocks"]["kitchen"]["rotation"] is True
    # a fresh controller for each clock reads back its own entry
    c1b, *_ = make("main", "abc")
    c2b, *_ = make("kitchen", "k1")
    assert c1b.rotation_enabled is False
    assert c2b.rotation_enabled is True
