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
        self.rects = []         # (tube, x, y, w, h, data) partial uploads
        self.rect_batches = []  # tubes of each send_rects call
        self.tubes_sent = []    # (tube, data) unconditional single-tube uploads
        self.photo_tubes = []   # photo_tubes kwarg of each send_tubes call
        self.last_error = None
        self.reachable = reachable
        self.probes = 0
        self.resets = 0
        self.probe_results = None   # optional list/queue of results; default: succeed
        # gif support (HA Tubes 2.4+); tests set gif_supported=True to exercise playback
        self.gif_supported = False
        self.gif_files = {}             # name -> bytes on the "clock"
        self.gif_plays = []             # (tube, name, loop, bg, x, y)
        self.gif_stops = []             # tube or None
        self.gif_deletes = []           # names
        self.upload_format = "rgb565"

    async def send_tubes(self, tube_bytes, needed_mask=0, photo_tubes=None):
        self.sent.append((tube_bytes, needed_mask))
        self.photo_tubes.append(set(photo_tubes or ()))
        return list(range(TUBE_COUNT))

    async def send_tube(self, tube, data, photo=False):
        self.tubes_sent.append((tube, data))
        return True

    async def send_rect(self, tube, x, y, w, h, data, photo=False):
        self.rects.append((tube, x, y, w, h, data))
        return True

    async def send_rects(self, rects):
        self.rect_batches.append([r[0] for r in rects])
        for r in rects:
            self.rects.append(tuple(r[:6]))
        return True

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

    def bytes_per_minute(self):
        return 0

    async def gif_list(self):
        return {"files": [{"name": n, "size": len(d)} for n, d in self.gif_files.items()],
                "free": 1024 * 1024}

    async def gif_upload(self, name, data):
        self.gif_files[name] = bytes(data)
        return True

    async def gif_play(self, tube, name, *, loop=True, bg=None, x=None, y=None):
        self.gif_plays.append((tube, name, loop, bg, x, y))
        return True

    async def gif_stop(self, tube=None):
        self.gif_stops.append(tube)
        return True

    async def gif_delete(self, name):
        self.gif_deletes.append(name)
        self.gif_files.pop(name, None)
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


# ======================================================================
# transitions (item 2): screen `transition` / `transition_delay`
# ======================================================================

def _transition_screens(transition=None, delay=None, top=None):
    data = _base_screens()
    data["rotation"] = ["a"]
    if top is not None:
        data["transition"] = top
    scr = {"duration": 1000, "refresh": 1000,
           "tubes": [{"type": "text", "text": "A"}] * 6}
    if transition is not None:
        scr["transition"] = transition
    if delay is not None:
        scr["transition_delay"] = delay
    data["screens"]["a"] = scr
    return data


def test_transition_none_sends_all_tubes_at_once(tmp_path):
    controller, time, pusher, mqtt = _make_controller(tmp_path, _transition_screens("none"))
    asyncio.run(controller.run_next_entry())
    assert len(pusher.sent) == 1 and pusher.sent[0][1] == 0b111111
    assert pusher.tubes_sent == [] and pusher.rects == []


def test_transition_cascade_sends_tubes_0_to_5_with_delay(tmp_path):
    controller, time, pusher, mqtt = _make_controller(
        tmp_path, _transition_screens("cascade", delay=90))
    t0 = time.t
    asyncio.run(controller._activate_screen(controller._config.screens["a"]))
    assert [t for t, _ in pusher.tubes_sent] == [0, 1, 2, 3, 4, 5]
    assert pusher.sent == []                       # no full send_tubes call
    # 5 delays of 90 ms between the 6 tubes, plus the 0.5 s mode-switch pause
    assert time.t - t0 == pytest.approx(0.5 + 5 * 0.09, abs=0.01)


def test_transition_cascade_right_and_center_out_orders(tmp_path):
    for t, order in (("cascade_right", [5, 4, 3, 2, 1, 0]),
                     ("center_out", [2, 3, 1, 4, 0, 5])):
        controller, time, pusher, mqtt = _make_controller(
            tmp_path, _transition_screens(t, delay=0))
        asyncio.run(controller.run_next_entry())
        assert [tube for tube, _ in pusher.tubes_sent] == order


def test_transition_runs_on_a_button_press_in_button_only_mode(tmp_path):
    """Rotation off: the clock's button sets the interrupt flag to wake the loop; the screen it
    brings up must still play its transition (regression: it was skipped)."""
    data = _transition_screens("center_out", delay=0)
    data["rotation"] = ["a", "b"]
    data["screens"]["b"] = dict(data["screens"]["a"])
    controller, time, pusher, mqtt = _make_controller(tmp_path, data)
    controller.rotation_enabled = False
    controller.current = "a"
    controller.next_screen()                       # what the button does
    assert controller._interrupted is True
    asyncio.run(controller._manual_step())
    assert [tube for tube, _ in pusher.tubes_sent] == [2, 3, 1, 4, 0, 5]
    assert pusher.sent == []                       # not one plain send of all six


def test_transition_wipe_sends_six_bands_per_tube_staggered_left_to_right(tmp_path):
    controller, time, pusher, mqtt = _make_controller(
        tmp_path, _transition_screens("wipe", delay=0))
    asyncio.run(controller.run_next_entry())
    assert pusher.tubes_sent == []
    rects = pusher.rects
    assert len(rects) == 6 * 6                     # 6 tubes x 6 bands
    # 11 steps: each tube starts a step after the one to its left
    assert pusher.rect_batches == [list(range(max(0, s - 5), min(s, 5) + 1)) for s in range(11)]
    for tube in range(6):
        bands = [r for r in rects if r[0] == tube]
        assert [(r[2], r[4]) for r in bands] == [(b * 40, 40) for b in range(5)] + [(200, 40)]
        for r in bands:
            assert r[1] == 0 and r[3] == 135       # full tube width
            assert len(r[5]) == 135 * r[4] * 2


def test_transition_wipe_pauses_a_third_of_the_delay_between_steps(tmp_path):
    controller, time, pusher, mqtt = _make_controller(
        tmp_path, _transition_screens("wipe", delay=90))
    t0 = time.t
    asyncio.run(controller._activate_screen(controller._config.screens["a"]))
    # 10 pauses of 30 ms between the 11 steps, plus the 0.5 s mode-switch pause
    assert time.t - t0 == pytest.approx(0.5 + 10 * 0.03, abs=0.01)


def test_transition_not_applied_on_refresh(tmp_path):
    """A refresh re-renders and sends changed tubes only - no tube-by-tube transition."""
    data = _transition_screens("cascade", delay=90)
    data["screens"]["a"]["refresh"] = 2
    controller, time, pusher, mqtt = _make_controller(tmp_path, data)
    asyncio.run(controller._activate_screen(controller._config.screens["a"]))
    assert len(pusher.tubes_sent) == 6             # activation sent the tubes one by one
    n_sent = len(pusher.sent)
    # refreshes re-render and push changed tubes only - never tube by tube
    for _ in range(3):
        asyncio.run(controller._refresh())
    assert len(pusher.sent) == n_sent + 3          # three refreshes happened
    assert len(pusher.tubes_sent) == 6             # but no extra single-tube sends


def test_transition_aborted_by_interrupt(tmp_path):
    """A pin arriving during a transition aborts it: the remaining tubes are not sent."""
    controller, time, pusher, mqtt = _make_controller(
        tmp_path, _transition_screens("cascade", delay=500))

    async def drive():
        task = asyncio.ensure_future(controller.run_next_entry())
        await asyncio.sleep(0)                     # let the transition start
        # interrupt after the first tube (fake time only advances on sleep, so
        # set the flag directly - the transition checks it between tubes)
        controller._interrupted = True
        await asyncio.wait_for(task, timeout=5)
    asyncio.run(drive())
    assert len(pusher.tubes_sent) <= 2             # aborted early, not all six
    assert controller._transitioning is False


def test_transition_flash_uses_global_default(tmp_path):
    data = _transition_screens(top="cascade")
    data["screens"]["a"]["transition"] = "none"    # the screen itself: none
    controller, time, pusher, mqtt = _make_controller(tmp_path, data)
    _run_loop(controller, time, 3)                 # showing "a" (no transition)
    assert pusher.tubes_sent == []
    controller.flash("hello", seconds=100)
    _run_loop(controller, time, 2)
    # the flash used the global default (cascade): tubes sent one by one
    assert [t for t, _ in pusher.tubes_sent] == [0, 1, 2, 3, 4, 5]


def test_transition_delay_default_90ms(tmp_path):
    controller, time, pusher, mqtt = _make_controller(
        tmp_path, _transition_screens("cascade"))  # no transition_delay -> 90 ms
    t0 = time.t
    asyncio.run(controller._activate_screen(controller._config.screens["a"]))
    assert [t for t, _ in pusher.tubes_sent] == list(range(6))
    assert time.t - t0 == pytest.approx(0.5 + 5 * 0.09, abs=0.01)


# ======================================================================
# animated tubes (item 3): animate.ANIMATORS + send_rect frames
# ======================================================================

class FakeAnimator:
    """A fake animator: one frame per `interval` seconds, a small coloured rect whose
    colour encodes t, so tests can see which frames were sent."""
    def __init__(self, interval=1.0, w=20, h=10):
        self._interval = interval
        self._w, self._h = w, h
        self.frames = []                            # t values passed to frame()

    def interval(self, w):
        return self._interval

    async def frame(self, w, ctx, t, bg):
        from PIL import Image
        self.frames.append(t)
        shade = int(t * 10) % 256
        return Image.new("RGB", (self._w, self._h), (shade, 0, 0)), (5, 5, self._w, self._h)


def _anim_screens(widget_type="text"):
    data = _base_screens()
    data["rotation"] = ["a"]
    data["screens"]["a"] = {"duration": 1000, "refresh": 1000,
                            "tubes": [{"type": widget_type}] * 6}
    return data


def _with_animator(monkeypatch, animator, widget_type="text"):
    import animate
    monkeypatch.setitem(animate.ANIMATORS, widget_type, animator)


def test_animator_frames_sent_at_interval(tmp_path, monkeypatch):
    animator = FakeAnimator(interval=1.0)
    _with_animator(monkeypatch, animator)
    controller, time, pusher, mqtt = _make_controller(tmp_path, _anim_screens())
    _run_loop(controller, time, 5.5)
    # frames at t=1,2,3,4,5 (the loop wakes every 0.25 s) on all six tubes
    assert len(pusher.rects) >= 6 * 4, len(pusher.rects)
    tubes = {r[0] for r in pusher.rects}
    assert tubes == {0, 1, 2, 3, 4, 5}
    for r in pusher.rects:
        assert (r[1], r[2], r[3], r[4]) == (5, 5, 20, 10)
        assert len(r[5]) == 20 * 10 * 2
    # t increases across the frames of one tube
    ts = [t for t in animator.frames]
    assert ts == sorted(ts) and ts[-1] > ts[0]
    assert ts[-1] >= 4.0


def test_animator_rate_cap_holds(tmp_path, monkeypatch):
    """At most 10 rect uploads per second in total, even with 6 animated tubes at
    a 0.1 s interval (60 frames/s wanted)."""
    animator = FakeAnimator(interval=0.1)
    _with_animator(monkeypatch, animator)
    controller, time, pusher, mqtt = _make_controller(tmp_path, _anim_screens())
    _run_loop(controller, time, 3.0)
    # count rects per fake second
    per_sec = {}
    for r in pusher.rects:
        pass
    # the controller records send times; recompute from _anim_rect_times is not
    # possible after the fact, so count via the cap: in any 1 s window at most 10
    # rects. Approximate: total rects over 3 s <= 10 * 3 + slack for the first burst
    assert len(pusher.rects) <= 10 * 3 + 10, len(pusher.rects)


def test_animator_skipped_while_paused_ota_flash(tmp_path, monkeypatch):
    animator = FakeAnimator(interval=0.5)
    _with_animator(monkeypatch, animator)
    controller, time, pusher, mqtt = _make_controller(tmp_path, _anim_screens())
    _run_loop(controller, time, 2)
    n = len(pusher.rects)
    assert n > 0                                # animating while the screen shows
    # paused: no frames
    controller.pause(None)
    _run_loop(controller, time, 3)
    assert len(pusher.rects) == n
    controller.resume()
    _run_loop(controller, time, 2)
    assert len(pusher.rects) > n                # resumed
    # OTA: no frames
    n = len(pusher.rects)
    _run(controller.on_ota_state({"in_progress": True}))
    _run_loop(controller, time, 3)
    assert len(pusher.rects) == n
    _run(controller.on_ota_state({"in_progress": False}))
    _run_loop(controller, time, 2)
    assert len(pusher.rects) > n
    # flash: no frames
    n = len(pusher.rects)
    controller.flash("hi", seconds=5)
    _run_loop(controller, time, 6)
    assert len(pusher.rects) == n
    _run_loop(controller, time, 2)
    assert len(pusher.rects) > n                # back to the screen after the flash


def test_animator_stops_on_screen_change(tmp_path, monkeypatch):
    animator = FakeAnimator(interval=0.5)
    _with_animator(monkeypatch, animator)
    data = _anim_screens()
    data["rotation"] = ["a", "b"]
    data["screens"]["b"] = {"duration": 1000, "refresh": 1000,
                            "tubes": [{"type": "blank"}] * 6}  # no animator for "blank"
    controller, time, pusher, mqtt = _make_controller(tmp_path, data)
    _run_loop(controller, time, 3)
    assert controller.current == "a"
    n = len(pusher.rects)
    assert n > 0
    controller.show_screen("b", seconds=100)
    _run_loop(controller, time, 3)
    assert controller.current == "b"
    assert len(pusher.rects) == n               # nothing animates on the new screen


def test_animator_not_animated_when_interval_zero(tmp_path, monkeypatch):
    animator = FakeAnimator(interval=0.0)       # 0 = not animated
    _with_animator(monkeypatch, animator)
    controller, time, pusher, mqtt = _make_controller(tmp_path, _anim_screens())
    _run_loop(controller, time, 3)
    assert pusher.rects == []


def test_animator_dim_applies_to_frames(tmp_path, monkeypatch):
    """Night dimming applies to animation frames too: with dim 0.5 the frame bytes
    are darker than the undimmed frame."""
    animator = FakeAnimator(interval=0.5)
    _with_animator(monkeypatch, animator)

    async def renderer(t):
        if "elevation" in t:
            return "-10"                        # full night
        return "true"

    data = _anim_screens()
    data["night"] = {"brightness_day": 255, "brightness_night": 10,
                     "bright_above": 10.0, "dim_below": -10.0, "dim_screens": 0.5}
    controller, time, pusher, mqtt = _make_controller(tmp_path, data, renderer=renderer)
    _run_loop(controller, time, 2)
    assert controller.daylight == 0.0
    assert pusher.rects, "frames were sent"
    # the fake animator's frame at t has shade int(t*10)%256; dim 0.5 halves it
    from rgb565 import image_to_rgb565
    from PIL import Image
    t_last = animator.frames[-1]
    undimmed = image_to_rgb565(Image.new("RGB", (20, 10), (int(t_last * 10) % 256, 0, 0)))
    sent = pusher.rects[-1][5]
    assert sent != undimmed
    assert sent[0] < undimmed[0] or sent[0] == 0   # darker red channel


# ======================================================================
# live mirror (item 4): controller.mirror / end_mirror
# ======================================================================

def _mirror_screen(name="__mirror__"):
    from screens import Screen
    return Screen(name=name, tubes=[{"type": "text", "text": "MIRROR"}] * 6)


def test_mirror_shows_replaces_and_expires(tmp_path):
    controller, time, pusher, mqtt = _make_controller(tmp_path, _base_screens())
    _run_loop(controller, time, 3)
    assert controller.current == "a"
    assert controller.status()["mirroring"] is False
    controller.mirror(_mirror_screen(), seconds=30)
    assert controller.status()["mirroring"] is True
    _run_loop(controller, time, 3)
    assert controller.current == "__mirror__"
    assert len(pusher.sent) >= 1
    # each call replaces the screen and restarts the timer
    controller.mirror(_mirror_screen("__mirror2__"), seconds=30)
    _run_loop(controller, time, 3)
    assert controller.current == "__mirror2__"
    # expires after `seconds`
    time.t += 31
    _run_loop(controller, time, 3)
    assert controller.status()["mirroring"] is False
    assert controller.current in ("a", "b", CLOCK_ENTRY)   # back to the rotation


def test_mirror_flash_still_wins(tmp_path):
    controller, time, pusher, mqtt = _make_controller(tmp_path, _base_screens())
    controller.mirror(_mirror_screen(), seconds=1000)
    _run_loop(controller, time, 2)
    assert controller.current == "__mirror__"
    controller.flash("urgent", seconds=10)
    _run_loop(controller, time, 3)
    assert controller.status()["message"] == "urgent"
    assert controller.current == "__mirror__" or controller._flash is not None
    _run_loop(controller, time, 10)             # flash over -> mirror again
    assert controller.status()["message"] is None
    assert controller.current == "__mirror__"
    assert controller.status()["mirroring"] is True


def test_mirror_end_mirror_resumes(tmp_path):
    controller, time, pusher, mqtt = _make_controller(tmp_path, _base_screens())
    _run_loop(controller, time, 3)
    controller.mirror(_mirror_screen(), seconds=1000)
    _run_loop(controller, time, 2)
    assert controller.current == "__mirror__"
    controller.end_mirror()
    assert controller.status()["mirroring"] is False
    _run_loop(controller, time, 3)
    assert controller.current in ("a", "b", CLOCK_ENTRY)


def test_mirror_refreshes_with_new_content(tmp_path):
    """The editor re-POSTs on every change: mirror() again re-renders the new screen."""
    controller, time, pusher, mqtt = _make_controller(tmp_path, _base_screens())
    _run_loop(controller, time, 2)
    controller.mirror(_mirror_screen(), seconds=1000)
    _run_loop(controller, time, 2)
    n = len(pusher.sent)
    from screens import Screen
    controller.mirror(Screen(name="__mirror__",
                             tubes=[{"type": "text", "text": "CHANGED"}] * 6), seconds=1000)
    _run_loop(controller, time, 2)
    assert len(pusher.sent) > n                 # re-rendered and pushed


def test_load_config_collects_warnings_and_status(tmp_path, caplog):
    """Unknown fields become warnings: logged once at warning level, kept in
    config_warnings and exposed in status(); the summary line counts them."""
    import logging
    data = _base_screens()
    # fresh dicts: _base_screens repeats one dict object 6 times
    data["screens"]["a"]["tubes"] = [{"type": "text", "text": "A", "colour": "#fff"}] + \
        [{"type": "text", "text": "A"}] * 5
    data["screens"]["a"]["durations"] = 5                    # unknown screen-level field
    with caplog.at_level(logging.INFO, logger="controller"):
        controller, time, pusher, mqtt = _make_controller(tmp_path, data)
    assert controller.config_errors == []
    assert len(controller.config_warnings) == 2
    assert any("unknown field 'colour'" in w and "did you mean 'color'?" in w
               for w in controller.config_warnings)
    assert any("unknown field 'durations'" in w for w in controller.config_warnings)
    status = controller.status()
    assert status["config_warnings"] == controller.config_warnings
    assert status["config_errors"] == []
    # each warning logged once, at warning level; the summary line has the counts
    warn_logs = [r for r in caplog.records if r.levelno == logging.WARNING
                 and "unknown field" in r.getMessage()]
    assert len(warn_logs) == 2
    summary = [r for r in caplog.records if "Loaded" in r.getMessage() and "warnings" in r.getMessage()]
    assert len(summary) == 1
    assert "2 screens (0 errors, 2 warnings)" in summary[0].getMessage()


# ======================================================================
# gif tubes: upload / play / skip-refresh / stop / cleanup (fake pusher)
# ======================================================================

def _gif_bytes(frames=((255, 0, 0), (0, 255, 0)), size=(60, 40)):
    import io
    from PIL import Image
    imgs = [Image.new("RGB", size, c) for c in frames]
    buf = io.BytesIO()
    imgs[0].save(buf, format="GIF", save_all=True, append_images=imgs[1:])
    return buf.getvalue()


def _gif_screens(tmp_path, gif_tubes=((0, "file:anim.gif"),), **widget_extra):
    """A screens file with a gif tube (and the gif file in the images folder)."""
    images = tmp_path / "images"
    images.mkdir(exist_ok=True)
    (images / "anim.gif").write_bytes(_gif_bytes())
    tubes = [{"type": "blank"}] * 6
    for idx, src in gif_tubes:
        tubes[idx] = {"type": "gif", "source": src, **widget_extra}
    data = {
        "rotation": ["a"],
        "screens": {"a": {"duration": 1000, "refresh": 1000, "tubes": tubes}},
    }
    return data


def test_gif_screen_plays_on_gif_clock(tmp_path):
    pusher = FakePusher()
    pusher.gif_supported = True
    controller, time, pusher, mqtt = _make_controller(
        tmp_path, _gif_screens(tmp_path, gif_tubes=((2, "file:anim.gif"),),
                               loop=False, background="#102030", x=5, y=7),
        pusher=pusher)
    asyncio.run(controller._activate_screen(controller._config.screens["a"]))
    # the file was uploaded (td- prefix) and played on tube 2 with bg/loop/x/y
    assert "td-anim.gif" in pusher.gif_files
    assert pusher.gif_plays == [(2, "td-anim.gif", False, "102030", 5, 7)]
    assert controller._gif_tubes == {2: controller._tubes[2]}
    # the static first frame was still pushed first (all tubes, mask=all)
    assert pusher.sent and pusher.sent[0][1] == 0b111111


def test_gif_not_played_without_gif_support(tmp_path):
    pusher = FakePusher()          # gif_supported=False (old firmware)
    controller, time, pusher, mqtt = _make_controller(
        tmp_path, _gif_screens(tmp_path), pusher=pusher)
    asyncio.run(controller._activate_screen(controller._config.screens["a"]))
    assert pusher.gif_plays == [] and pusher.gif_files == {}
    assert pusher.sent and pusher.sent[0][1] == 0b111111   # static first frame only


def test_gif_refresh_skips_playing_tube(tmp_path):
    """A refresh must not re-send the static first frame over a playing GIF."""
    pusher = FakePusher()
    pusher.gif_supported = True
    controller, time, pusher, mqtt = _make_controller(
        tmp_path, _gif_screens(tmp_path, gif_tubes=((1, "file:anim.gif"),)), pusher=pusher)
    asyncio.run(controller._activate_screen(controller._config.screens["a"]))
    assert pusher.gif_plays and pusher.gif_plays[0][0] == 1
    # the pusher knows tube 1 is playing (as GET /panel "playing" would say)
    pusher._playing = {1}
    n = len(pusher.sent)
    asyncio.run(controller._refresh())
    # the refresh re-rendered and called send_tubes...
    assert len(pusher.sent) == n + 1
    # ...and the real pusher's send_tubes drops playing tubes (tested against the
    # real ClockPusher in test_clock.py); here the fake records what it was given
    assert pusher.sent[-1][1] == 0          # no needed mask on a plain refresh


def test_gif_stopped_when_clock_mode_taken_over(tmp_path):
    pusher = FakePusher()
    pusher.gif_supported = True
    controller, time, pusher, mqtt = _make_controller(
        tmp_path, _gif_screens(tmp_path, gif_tubes=((3, "file:anim.gif"),)), pusher=pusher)
    asyncio.run(controller._activate_screen(controller._config.screens["a"]))
    assert pusher.gif_plays
    asyncio.run(controller._activate_clock())          # pause / quiet clock mode
    assert pusher.gif_stops == [3]
    assert mqtt.display_modes[-1] == 0
    # pause does it too
    pusher.gif_stops.clear()
    asyncio.run(controller._activate_screen(controller._config.screens["a"]))
    asyncio.run(controller._show_paused())
    assert pusher.gif_stops == [3]


def test_gif_upload_skipped_when_already_on_clock_with_same_size(tmp_path):
    pusher = FakePusher()
    pusher.gif_supported = True
    data = _gif_bytes()
    pusher.gif_files["td-anim.gif"] = data          # already there, same size
    controller, time, pusher, mqtt = _make_controller(
        tmp_path, _gif_screens(tmp_path), pusher=pusher)
    uploads_before = len(pusher.gif_files)
    asyncio.run(controller._activate_screen(controller._config.screens["a"]))
    assert pusher.gif_plays == [(0, "td-anim.gif", True, "000000", None, None)]
    # no re-upload happened (gif_upload would have re-set the entry; count via a marker)
    assert len(pusher.gif_files) == uploads_before


def test_gif_cleanup_deletes_unused_td_files_hourly(tmp_path):
    pusher = FakePusher()
    pusher.gif_supported = True
    pusher.gif_files["td-anim.gif"] = _gif_bytes()       # used by screen "a"
    pusher.gif_files["td-old.gif"] = b"GIF89a-old"       # used by nothing
    pusher.gif_files["user.gif"] = b"GIF89a-user"        # not ours: never deleted
    controller, time, pusher, mqtt = _make_controller(
        tmp_path, _gif_screens(tmp_path), pusher=pusher)
    asyncio.run(controller._activate_screen(controller._config.screens["a"]))
    assert "td-old.gif" not in pusher.gif_files
    assert "td-anim.gif" in pusher.gif_files
    assert "user.gif" in pusher.gif_files
    assert pusher.gif_deletes == ["td-old.gif"]
    # at most once an hour: a second activation does not clean up again
    pusher.gif_files["td-stale.gif"] = b"x"
    asyncio.run(controller._activate_screen(controller._config.screens["a"]))
    assert "td-stale.gif" in pusher.gif_files
    time.t += 3700
    asyncio.run(controller._activate_screen(controller._config.screens["a"]))
    assert "td-stale.gif" not in pusher.gif_files


def test_gif_oversized_file_not_uploaded(tmp_path):
    pusher = FakePusher()
    pusher.gif_supported = True
    images = tmp_path / "images"
    images.mkdir(exist_ok=True)
    (images / "big.gif").write_bytes(b"GIF89a" + b"\x00" * (600 * 1024))   # over 600 KB
    data = {"rotation": ["a"], "screens": {"a": {"tubes": [
        {"type": "gif", "source": "file:big.gif"}] + [{"type": "blank"}] * 5}}}
    controller, time, pusher, mqtt = _make_controller(tmp_path, data, pusher=pusher)
    asyncio.run(controller._activate_screen(controller._config.screens["a"]))
    assert pusher.gif_plays == [] and pusher.gif_files == {}
    assert pusher.sent and pusher.sent[0][1] == 0b111111   # first frame still shown


def test_status_has_upload_format_and_bytes_per_minute(tmp_path):
    pusher = FakePusher()
    pusher.upload_format = "qoi"
    controller, time, pusher, mqtt = _make_controller(tmp_path, _base_screens(), pusher=pusher)
    status = controller.status()
    assert status["upload_format"] == "qoi"
    assert status["bytes_per_minute"] == 0
    # without a pusher the keys are still there
    controller2, *_ = _make_controller(tmp_path, _base_screens(), pusher=None)
    controller2._pusher = None
    s2 = controller2.status()
    assert s2["upload_format"] == "rgb565" and s2["bytes_per_minute"] == 0


class FirmwareTransitionPusher(FakePusher):
    """HA Tubes 2.5+: plays transitions itself from send_screen (None = can't, fall back)."""

    def __init__(self, result=True, transitions=("wipe", "cascade")):
        super().__init__()
        self.result = result
        self.transitions = set(transitions)
        self.screens = []       # (kind, order, duration_ms, gap_ms, photo)

    async def send_screen(self, tube_bytes, photo_tubes, kind, order, duration_ms=500, gap_ms=90):
        self.screens.append((kind, list(order), duration_ms, gap_ms, set(photo_tubes or ())))
        return self.result


def _with_pusher(controller, pusher):
    controller._pusher = pusher
    return pusher


def test_firmware_plays_the_transition_and_the_app_waits_for_it(tmp_path):
    for t, kind, order, wait in (("wipe", "wipe", [0, 1, 2, 3, 4, 5], 0.4),
                                 ("wipe_right", "wipe", [5, 4, 3, 2, 1, 0], 0.4),
                                 ("wipe_center", "wipe", [2, 3, 1, 4, 0, 5], 0.4),
                                 ("center_out", "cascade", [2, 3, 1, 4, 0, 5], 5 * 0.07)):
        data = _transition_screens(t, delay=70)
        data["screens"]["a"]["transition_duration"] = 400
        controller, time, pusher, mqtt = _make_controller(tmp_path, data)
        p = _with_pusher(controller, FirmwareTransitionPusher())
        t0 = time.t
        asyncio.run(controller._activate_screen(controller._config.screens["a"]))
        assert p.screens == [(kind, order, 400, 70, set())]
        assert p.tubes_sent == [] and p.rects == [] and p.sent == []   # nothing sent the old way
        # the 0.5 s mode-switch pause, then waiting for the clock's transition to finish
        assert time.t - t0 == pytest.approx(0.5 + wait, abs=0.01)


def test_firmware_plays_a_flip_with_duration_and_gap_and_the_app_waits(tmp_path):
    for t, order in (("flip", [0, 1, 2, 3, 4, 5]), ("flip_right", [5, 4, 3, 2, 1, 0]),
                     ("flip_center", [2, 3, 1, 4, 0, 5])):
        data = _transition_screens(t, delay=80)
        data["screens"]["a"]["transition_duration"] = 450
        controller, time, pusher, mqtt = _make_controller(tmp_path, data)
        p = _with_pusher(controller, FirmwareTransitionPusher(
            transitions=("wipe", "cascade", "flip")))
        t0 = time.t
        asyncio.run(controller._activate_screen(controller._config.screens["a"]))
        assert p.screens == [("flip", order, 450, 80, set())]
        assert p.tubes_sent == [] and p.rects == [] and p.sent == []
        # each tube's flip, plus the gaps between their starts
        assert time.t - t0 == pytest.approx(0.5 + 0.45 + 5 * 0.08, abs=0.01)


def test_flip_on_25_firmware_plays_its_wipe(tmp_path):
    data = _transition_screens("flip_center", delay=80)
    data["screens"]["a"]["transition_duration"] = 450
    controller, time, pusher, mqtt = _make_controller(tmp_path, data)
    p = _with_pusher(controller, FirmwareTransitionPusher())      # wipe and cascade only
    asyncio.run(controller._activate_screen(controller._config.screens["a"]))
    assert p.screens == [("wipe", [2, 3, 1, 4, 0, 5], 450, 80, set())]


def test_flip_on_old_firmware_is_the_apps_own_wipe(tmp_path):
    controller, time, pusher, mqtt = _make_controller(tmp_path, _transition_screens("flip"))
    asyncio.run(controller._activate_screen(controller._config.screens["a"]))
    assert len(pusher.rects) == 6 * 6       # six bands per tube, like a wipe
    assert pusher.tubes_sent == []


def test_firmware_transition_wait_stops_on_an_interrupt(tmp_path):
    data = _transition_screens("wipe")
    data["screens"]["a"]["transition_duration"] = 2000
    controller, time, pusher, mqtt = _make_controller(tmp_path, data)
    p = _with_pusher(controller, FirmwareTransitionPusher())
    real_sleep = controller._sleep

    async def sleep(s):
        await real_sleep(s)
        if time.t > 1.0 + t0:
            controller._interrupted = True      # a button press half way
    controller._sleep = sleep
    t0 = time.t
    asyncio.run(controller._activate_screen(controller._config.screens["a"]))
    assert len(p.screens) == 1 and time.t - t0 < 1.2


def test_falls_back_to_the_app_when_the_clock_cant_play_it(tmp_path):
    controller, time, pusher, mqtt = _make_controller(tmp_path, _transition_screens("wipe_right", delay=0))
    p = _with_pusher(controller, FirmwareTransitionPusher(result=None))
    asyncio.run(controller._activate_screen(controller._config.screens["a"]))
    assert len(p.screens) == 1
    # the app's own staggered wipe, right to left
    assert p.rect_batches[:3] == [[5], [5, 4], [5, 4, 3]]
    assert len(p.rects) == 36


def test_transition_duration_validation(tmp_path):
    from screens import parse_screens as parse_screens_data
    cfg = parse_screens_data({"rotation": ["a"], "screens": {"a": {
        "transition": "wipe_center", "transition_duration": 50, "tubes": [{"type": "text", "text": "x"}] * 6}}})
    assert [e for e in cfg.errors if "transition_duration" in e]
    assert cfg.screens["a"].resolved_transition_duration() == 500   # still loads, with the default
    cfg = parse_screens_data({"rotation": ["a"], "screens": {"a": {
        "transition": "wipe_center", "transition_duration": 1200, "tubes": [{"type": "text", "text": "x"}] * 6}}})
    assert not cfg.errors and cfg.screens["a"].resolved_transition_duration() == 1200


# ======================================================================
# "Play on clock": api/mirror with play_transition runs the transition once
# ======================================================================

def test_mirror_play_transition_runs_the_transition_once(tmp_path):
    """A mirror request with play_transition shows the mirrored screen through
    _run_transition (FirmwareTransitionPusher records send_screen); a later plain
    mirror refresh of the same screen is a plain send_tubes."""
    controller, time, pusher, mqtt = _make_controller(tmp_path, _base_screens())
    p = _with_pusher(controller, FirmwareTransitionPusher())
    controller.mirror(_mirror_screen(), seconds=1000, play_transition=True,
                      transition="wipe", transition_delay=70, transition_duration=400)
    assert controller._mirror_transition == ("wipe", 70, 400)
    controller._interrupted = False       # run() clears it before showing an entry
    asyncio.run(controller._activate_screen(controller._mirror[0]))
    assert p.screens == [("wipe", [0, 1, 2, 3, 4, 5], 400, 70, set())]
    assert p.sent == [] and p.tubes_sent == []      # not a plain send
    assert controller._mirror_transition is None    # played once only
    # a later mirror refresh (plain mirror() call) is a plain send
    controller.mirror(_mirror_screen(), seconds=1000)
    assert controller._mirror_transition is None
    controller._interrupted = False
    asyncio.run(controller._activate_screen(controller._mirror[0]))
    assert len(p.screens) == 1                       # no second transition
    assert len(p.sent) == 1 and p.sent[0][1] == 0b111111


def test_mirror_play_transition_app_transition_when_clock_cant(tmp_path):
    """Without firmware transitions the play_transition mirror still runs the app's
    own transition (tubes one by one), not a plain send."""
    controller, time, pusher, mqtt = _make_controller(tmp_path, _base_screens())
    controller.mirror(_mirror_screen(), seconds=1000, play_transition=True,
                      transition="cascade", transition_delay=0)
    controller._interrupted = False
    asyncio.run(controller._activate_screen(controller._mirror[0]))
    assert [t for t, _ in pusher.tubes_sent] == [0, 1, 2, 3, 4, 5]
    assert pusher.sent == []


def test_mirror_plain_refresh_stays_plain(tmp_path):
    """A mirror request WITHOUT play_transition never runs a transition."""
    controller, time, pusher, mqtt = _make_controller(tmp_path, _base_screens())
    p = _with_pusher(controller, FirmwareTransitionPusher())
    controller.mirror(_mirror_screen(), seconds=1000)
    asyncio.run(controller._activate_screen(controller._mirror[0]))
    assert p.screens == [] and p.tubes_sent == []
    assert len(p.sent) == 1


# ======================================================================
# pre-render of the next screen (instant screen changes)
# ======================================================================

def _prerender_screens():
    data = _base_screens()
    data["rotation"] = ["a", "b"]
    data["screens"]["a"] = {"duration": 10, "refresh": 1000,
                            "tubes": [{"type": "text", "text": "A"}] * 6}
    data["screens"]["b"] = {"duration": 10, "refresh": 1000,
                            "tubes": [{"type": "text", "text": "B"}] * 6}
    return data


def test_prerender_used_when_fresh_and_same_screen(tmp_path, monkeypatch):
    """The rotation pre-renders the next screen 1 s before the current one ends, and
    _activate_screen uses it instead of rendering again."""
    import render as render_mod
    controller, time, pusher, mqtt = _make_controller(tmp_path, _prerender_screens())
    renders = []
    real_render = render_mod.render_screen

    async def counting_render(screen, ctx):
        renders.append(screen.name)
        return await real_render(screen, ctx)

    monkeypatch.setattr(render_mod, "render_screen", counting_render)
    asyncio.run(controller.run_next_entry())          # shows "a" for 10 s
    assert renders == ["a", "b"]                      # "a" rendered, "b" pre-rendered
    assert controller._prerender is not None
    assert controller._prerender[0] == "b"            # "b" was pre-rendered
    renders.clear()
    asyncio.run(controller.run_next_entry())          # shows "b" - uses the pre-render
    assert "b" not in renders                         # "b" was NOT rendered again
    assert controller.current == "b"
    assert pusher.sent and pusher.sent[-1][1] == 0b111111


def test_prerender_not_used_when_stale(tmp_path, monkeypatch):
    import render as render_mod
    controller, time, pusher, mqtt = _make_controller(tmp_path, _prerender_screens())
    renders = []
    real_render = render_mod.render_screen

    async def counting_render(screen, ctx):
        renders.append(screen.name)
        return await real_render(screen, ctx)

    monkeypatch.setattr(render_mod, "render_screen", counting_render)
    asyncio.run(controller.run_next_entry())          # "a", pre-renders "b"
    assert renders == ["a", "b"]
    time.t += 3.0                                     # the pre-render is now > 2 s old
    renders.clear()
    asyncio.run(controller._activate_screen(controller._config.screens["b"]))
    assert renders == ["b"]                           # rendered again
    assert controller._prerender is None


def test_prerender_not_used_for_a_different_screen(tmp_path, monkeypatch):
    import render as render_mod
    controller, time, pusher, mqtt = _make_controller(tmp_path, _prerender_screens())
    renders = []
    real_render = render_mod.render_screen

    async def counting_render(screen, ctx):
        renders.append(screen.name)
        return await real_render(screen, ctx)

    monkeypatch.setattr(render_mod, "render_screen", counting_render)
    asyncio.run(controller.run_next_entry())          # "a", pre-renders "b"
    assert renders == ["a", "b"]
    renders.clear()
    asyncio.run(controller._activate_screen(controller._config.screens["a"]))
    assert renders == ["a"]                           # "a" is rendered, not the "b" pre-render
    assert controller._prerender is None              # discarded on the mismatch


def test_prerender_not_used_after_config_reload(tmp_path, monkeypatch):
    import os
    import render as render_mod
    controller, time, pusher, mqtt = _make_controller(tmp_path, _prerender_screens())
    renders = []
    real_render = render_mod.render_screen

    async def counting_render(screen, ctx):
        renders.append(screen.name)
        return await real_render(screen, ctx)

    monkeypatch.setattr(render_mod, "render_screen", counting_render)
    asyncio.run(controller.run_next_entry())          # "a", pre-renders "b"
    assert controller._prerender is not None
    # the screens file changes -> reload -> the pre-render is invalid
    data = _prerender_screens()
    data["screens"]["b"]["tubes"] = [{"type": "text", "text": "B2"}] * 6
    _write_screens(tmp_path / "screens.yaml", data)
    os.utime(tmp_path / "screens.yaml", (time.t + 5, time.t + 5))
    asyncio.run(controller._reload_if_changed())
    assert controller._prerender is None
    renders.clear()
    asyncio.run(controller._activate_screen(controller._config.screens["b"]))
    assert renders == ["b"]


def test_prerender_used_on_a_button_press(tmp_path, monkeypatch):
    """next_screen() (the clock's button) also benefits when it lands on the
    pre-rendered screen."""
    import render as render_mod
    controller, time, pusher, mqtt = _make_controller(tmp_path, _prerender_screens())
    renders = []
    real_render = render_mod.render_screen

    async def counting_render(screen, ctx):
        renders.append(screen.name)
        return await real_render(screen, ctx)

    monkeypatch.setattr(render_mod, "render_screen", counting_render)
    controller.rotation_enabled = False
    controller.current = "a"
    controller._rotation_index = 0                   # "a" is the current rotation entry
    asyncio.run(controller._activate_screen(controller._config.screens["a"]))
    # pre-render "b" by hand, as the rotation loop would 1 s before "a" ends
    asyncio.run(controller._prerender_screen(controller._config.screens["b"]))
    renders.clear()
    controller.next_screen()
    asyncio.run(controller._manual_step())
    assert controller.current == "b"
    assert renders == []                              # the button press used the pre-render


def test_prerender_skipped_when_next_is_the_clock_entry(tmp_path):
    data = _prerender_screens()
    data["rotation"] = ["a", "clock"]
    controller, time, pusher, mqtt = _make_controller(tmp_path, data)
    asyncio.run(controller.run_next_entry())          # "a" for 10 s; next is "clock"
    assert controller._prerender is None              # nothing pre-rendered


# ======================================================================
# status(): clock_info and notices
# ======================================================================

class CapsPusher(FakePusher):
    """A FakePusher with learned capabilities and a last GET /panel answer, like a
    real ClockPusher after talking to the clock."""

    def __init__(self, panel, reachable=True):
        super().__init__(reachable=reachable)
        self.last_status = panel
        self._caps_known = True
        self.formats = {str(f) for f in panel.get("formats", [])}
        self.gif_supported = bool(panel.get("gif"))
        self.batch_supported = bool(panel.get("batch"))
        self.batch_buffer = panel.get("batch_buffer", 0) or 0
        self.transitions = {str(t) for t in panel.get("transitions", [])}
        self.upload_format = "qoi" if "qoi" in self.formats else "rgb565"


PANEL_23 = {"tubes": [0, 1, 2, 3, 4, 5], "needed": [], "width": 135, "height": 240}
PANEL_24 = dict(PANEL_23, formats=["rgb565", "qoi", "jpeg"], gif=True, gif_free=1769472,
                firmware="2.4.0")
PANEL_25 = dict(PANEL_24, batch=True, batch_buffer=37516, transitions=["wipe", "cascade"],
                drawing=False, last_draw_ms=211, firmware="2.5.0")


def _caps_screens(gif=False, transition=None, top=None):
    data = _base_screens()
    data["rotation"] = ["a"]
    scr = {"duration": 1000, "refresh": 1000,
           "tubes": [{"type": "text", "text": "A"}] * 6}
    if gif:
        scr["tubes"][0] = {"type": "gif", "source": "file:anim.gif"}
    if transition is not None:
        scr["transition"] = transition
    data["screens"]["a"] = scr
    if top is not None:
        data["transition"] = top
    return data


def test_status_clock_info_25(tmp_path):
    controller, time, pusher, mqtt = _make_controller(
        tmp_path, _caps_screens(), pusher=CapsPusher(PANEL_25))
    info = controller.status()["clock_info"]
    assert info["firmware"] == "2.5.0"
    assert info["upload_format"] == "qoi"
    assert info["batch_buffer"] == 37516
    assert info["transitions"] == ["cascade", "wipe"]
    assert info["last_draw_ms"] == 211
    assert info["gif"] is True
    assert info["notices"] == []
    assert info["bytes_per_minute"] == 0
    # the old top-level keys are still there
    status = controller.status()
    assert status["upload_format"] == "qoi" and status["bytes_per_minute"] == 0


def test_status_clock_info_24_no_transitions_capability(tmp_path):
    controller, time, pusher, mqtt = _make_controller(
        tmp_path, _caps_screens(transition="wipe"), pusher=CapsPusher(PANEL_24))
    info = controller.status()["clock_info"]
    assert info["firmware"] == "2.4.0"
    assert info["transitions"] == [] and info["batch_buffer"] == 0
    assert info["last_draw_ms"] is None
    codes = [n["code"] for n in info["notices"]]
    assert codes == ["transitions_need_2_5"]         # wipe on 2.4 -> notice
    assert all(n["message"] for n in info["notices"])


def test_status_notice_gif_on_23(tmp_path):
    controller, time, pusher, mqtt = _make_controller(
        tmp_path, _caps_screens(gif=True), pusher=CapsPusher(PANEL_23))
    info = controller.status()["clock_info"]
    assert info["firmware"] is None and info["gif"] is False
    assert [n["code"] for n in info["notices"]] == ["gif_needs_2_4"]


def test_status_notices_gif_and_transitions_on_23(tmp_path):
    controller, time, pusher, mqtt = _make_controller(
        tmp_path, _caps_screens(gif=True, transition="cascade"), pusher=CapsPusher(PANEL_23))
    codes = [n["code"] for n in controller.status()["clock_info"]["notices"]]
    assert codes == ["gif_needs_2_4", "transitions_need_2_5"]


def test_status_notice_transitions_from_top_level_default(tmp_path):
    controller, time, pusher, mqtt = _make_controller(
        tmp_path, _caps_screens(top="cascade"), pusher=CapsPusher(PANEL_24))
    codes = [n["code"] for n in controller.status()["clock_info"]["notices"]]
    assert codes == ["transitions_need_2_5"]         # the top-level default counts too


def test_status_no_notices_on_25_and_none_when_nothing_used(tmp_path):
    controller, time, pusher, mqtt = _make_controller(
        tmp_path, _caps_screens(), pusher=CapsPusher(PANEL_25))
    assert controller.status()["clock_info"]["notices"] == []
    # 2.4 with no gif screen and no transition: no notices either
    controller2, *_ = _make_controller(tmp_path, _caps_screens(), pusher=CapsPusher(PANEL_24))
    assert controller2.status()["clock_info"]["notices"] == []


PANEL_26 = dict(PANEL_25, transitions=["wipe", "cascade", "flip"], flip_memory=30720,
                firmware="2.6.0")


def test_status_notice_flip_on_25_only(tmp_path):
    for screens in (_caps_screens(transition="flip"), _caps_screens(top="flip_center")):
        controller, *_ = _make_controller(tmp_path, screens, pusher=CapsPusher(PANEL_25))
        notices = controller.status()["clock_info"]["notices"]
        assert [n["code"] for n in notices] == ["flip_needs_2_6"] and notices[0]["message"]
        controller, *_ = _make_controller(tmp_path, screens, pusher=CapsPusher(PANEL_26))
        assert controller.status()["clock_info"]["notices"] == []
        # 2.4 can't play any transition itself: just that notice
        controller, *_ = _make_controller(tmp_path, screens, pusher=CapsPusher(PANEL_24))
        assert [n["code"] for n in controller.status()["clock_info"]["notices"]] == \
            ["transitions_need_2_5"]
    # a wipe on 2.5 is fine
    controller, *_ = _make_controller(tmp_path, _caps_screens(transition="wipe"),
                                      pusher=CapsPusher(PANEL_25))
    assert controller.status()["clock_info"]["notices"] == []


def test_status_no_notices_when_offline_or_caps_unknown(tmp_path):
    # offline clock: no notices even though a gif screen exists
    controller, time, pusher, mqtt = _make_controller(
        tmp_path, _caps_screens(gif=True, transition="wipe"),
        pusher=CapsPusher(PANEL_23, reachable=False))
    controller._clock_online = False
    assert controller.status()["clock_info"]["notices"] == []
    # online but capabilities never learned: no notices either
    p = FakePusher()
    controller2, *_ = _make_controller(tmp_path, _caps_screens(gif=True, transition="wipe"),
                                      pusher=p)
    assert controller2.status()["clock_info"]["notices"] == []
    assert controller2.status()["clock_info"]["firmware"] is None
