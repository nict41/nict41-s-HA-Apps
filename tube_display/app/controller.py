"""The controller: rotation scheduler, screen activation, interrupts.

One Controller per clock (see settings.ClockSettings). Time and sleeping are
injectable (`now`, `sleep`) so tests can drive the rotation with a fake clock.
The loop:

- re-reads screens.yaml when its mtime changes,
- applies night mode (brightness + quiet hours) about every 60 s - at the top of
  the loop AND inside every waiting loop (screen, clock entry, pause, quiet
  hours, flash, rotation-off), so the brightness follows the sun even during
  long pauses and quiet hours,
- honours, in priority order: OTA in progress (nothing is sent) > flash
  message > pinned screen (POST /api/show, MQTT select) > pause > quiet hours
  > rotation (or the current screen when rotation is off; a flash/OTA that
  ends there re-activates that screen immediately),
- cycles through the clock's rotation (`rotation_for(name)`), skipping screens
  whose `when` is not "true", showing each for its duration and re-rendering
  every `refresh`.

While the clock is unreachable the pusher is probed with backoff instead of
pushing on every refresh. `on_template_change()` (live updates) makes the
waiting loops re-render within 0.25 s, throttled to `live_min_interval`.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import json
import os
import time
from typing import Awaitable, Callable

from PIL import Image

import night as night_mod
import render as render_mod
import animate as animate_mod
from clock import DISPLAY_CLOCK, DISPLAY_PANELS, ClockPusher
from rgb565 import STRIP_H, STRIP_W, TUBE_COUNT, TUBE_H, TUBE_W, image_to_rgb565, strip_to_tube_bytes
from screens import (CLOCK_ENTRY, DEFAULT_TRANSITION, DEFAULT_TRANSITION_DELAY, Screen,
                     ScreensConfig, ScreensError, load_screens)
from settings import ClockSettings

_LOG = logging.getLogger(__name__)

ALL_TUBES_MASK = 0b111111
NIGHT_TICK = 60.0          # seconds between night-mode (brightness) updates
OTA_TIMEOUT = 600.0        # an OTA that never reports "done" is given up after 10 min
PROBE_BACKOFF = (5.0, 10.0, 20.0, 40.0, 60.0)   # then 60 s forever
SUN_ELEVATION_TEMPLATE = "{{ state_attr('sun.sun','elevation') }}"
# tube order of each transition (tubes are sent one at a time, ~218 ms each)
TRANSITION_ORDERS = {
    "cascade": [0, 1, 2, 3, 4, 5],
    "cascade_right": [5, 4, 3, 2, 1, 0],
    "center_out": [2, 3, 1, 4, 0, 5],
}
WIPE_BANDS = 6             # a wipe draws each tube top to bottom in this many bands
WIPE_BAND_H = TUBE_H // WIPE_BANDS   # 40 rows per band
ANIM_MAX_RECTS_PER_SEC = 10         # cap on partial (send_rect) uploads per second
MIRROR_DEFAULT_SECONDS = 120.0
MIRROR_MAX_SECONDS = 600.0


class Controller:
    def __init__(self, settings, session, ha, pusher: ClockPusher | None = None, mqtt=None,
                 renderer: Callable[[str], Awaitable[str]] | None = None,
                 now: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 clock: ClockSettings | None = None) -> None:
        self.settings = settings
        self._session = session
        self._ha = ha
        self._pusher = pusher
        self._mqtt = mqtt
        self._now = now
        self._sleep = sleep
        self._renderer = renderer or (ha.render_template if ha else None)
        self._ctx = render_mod.RenderContext(self._renderer, session=session, ha=ha)
        self._ctx.images_dir = os.path.join(os.path.dirname(settings.screens_file) or ".", "images")

        # which clock this controller drives (None -> the primary/legacy one)
        self.clock = clock if clock is not None else settings.primary_clock
        self.name = self.clock.name if self.clock is not None else "main"

        self._config: ScreensConfig | None = None
        self._mtime: float | None = None
        self.current: str | None = None          # entry shown now ("clock" or a screen name)
        self.rotation_enabled = self._load_rotation_enabled()
        self._advance = False                     # button pressed while rotation is off
        self._pin: tuple[str, float | None] | None = None   # (entry, expiry; None = forever)
        self._rotation_index = -1
        self._interrupted = False
        self._stop = False
        self.clock_state: dict = {}               # last MQTT state from the clock
        self.last_error: str | None = None
        self.last_strip: Image.Image | None = None
        self.config_errors: list[str] = []
        self.screens_loaded_at: float | None = None

        # pause / flash / health
        self._paused = False
        self._pause_until: float | None = None    # None = until resume()
        self._pause_shown = False                 # display=0 published for this pause yet?
        self._flash: dict | None = None           # {"message","expiry","icon","color","prev"}
        self._reshow: str | None = None           # entry to re-show after a flash/OTA
        self._clock_online = True                 # last availability from the clock
        self._ota = False
        self._ota_since = 0.0
        self._next_probe = 0.0                    # monotonic time of the next reachability probe
        self._probe_failures = 0

        # night mode
        self.daylight = 1.0                       # 0..1, from the sun elevation
        self._brightness: int | None = None       # last brightness sent to the clock
        self._last_night_tick = -1e9
        self._quiet_shown = False
        self.local_now: Callable[[], datetime.datetime] = datetime.datetime.now

        # live updates (another module calls on_template_change when a template changes)
        self.live = False
        self.live_min_interval = 1.0
        self._live_dirty = False
        self._last_live = -1e9

        # live mirror (the editor's "Show on clock"): an in-memory screen shown like a
        # pinned one, just below a flash; each mirror() call replaces it and restarts
        self._mirror: tuple[Screen, float] | None = None   # (screen, expiry)

        # per-screen-show state: resolved tube widgets (for animators) and the
        # transition currently running (None when idle)
        self._tubes: list[dict | None] | None = None       # resolved widgets of the showing screen
        self._screen_since = 0.0                           # when the showing screen became active
        self._anim_next: dict[int, float] = {}             # tube -> next frame due (monotonic)
        self._anim_rect_times: list[float] = []            # recent send_rect times (rate cap)
        self._transitioning = False

    @property
    def mqtt_id(self) -> str:
        return self.clock.mqtt_id if self.clock is not None else self.settings.clock_mqtt_id

    def set_pusher(self, pusher: ClockPusher) -> None:
        self._pusher = pusher

    def set_mqtt(self, mqtt) -> None:
        self._mqtt = mqtt

    # ------------------------------------------------------------------
    # config
    # ------------------------------------------------------------------

    def load_config(self, force: bool = False) -> ScreensConfig | None:
        path = self.settings.screens_file
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = None
        if not force and self._config is not None and mtime == self._mtime:
            return self._config
        self._mtime = mtime
        try:
            cfg = load_screens(path)
        except (ScreensError, OSError) as err:
            _LOG.error("Could not load screens file %s: %s", path, err)
            self.config_errors = [str(err)]
            self.last_error = f"screens file: {err}"
            return self._config
        self._config = cfg
        self.config_errors = list(cfg.errors)
        for e in cfg.errors:
            _LOG.error("Screens file: %s (screen skipped)", e)
        if cfg.errors:
            self.last_error = "; ".join(cfg.errors)
        self.screens_loaded_at = self._now()
        _LOG.info("Loaded %d screen(s), rotation %s", len(cfg.screens), cfg.rotation)
        return cfg

    async def _reload_if_changed(self) -> None:
        old_names = set(self._config.screens) if self._config else set()
        cfg = self.load_config()
        if cfg is not None and set(cfg.screens) != old_names and self._mqtt is not None:
            # Awaited, so the select's options are up to date before the next screen is shown
            await self._mqtt.publish_discovery()

    def _rotation(self) -> list[str]:
        """This clock's rotation (its `clocks:` override, else the global one)."""
        cfg = self._config
        return cfg.rotation_for(self.name) if cfg is not None else []

    def _clock_duration(self) -> int:
        cfg = self._config
        if cfg is not None:
            d = cfg.clock_duration_for(self.name)
            if d is not None:
                return d
        return self.settings.default_duration

    # ------------------------------------------------------------------
    # control API (called from HTTP API / MQTT)
    # ------------------------------------------------------------------

    def show_screen(self, name: str, seconds: float | None = None) -> None:
        """Interrupt the rotation with `name` for `seconds` (None = until changed).
        Also ends a pause."""
        if name != CLOCK_ENTRY and self._config is not None and name not in self._config.screens:
            raise KeyError(name)
        expiry = None if seconds is None else self._now() + seconds
        self._pin = (name, expiry)
        self._paused = False
        self._pause_until = None
        self._reshow = None
        self._interrupted = True
        _LOG.info("Pinned screen '%s'%s", name,
                  f" for {seconds:g}s" if seconds is not None else "")

    def clear_pin(self) -> None:
        """End pin, pause and flash - back to the rotation."""
        self._pin = None
        self._paused = False
        self._pause_until = None
        self._flash = None
        self._reshow = None
        self._interrupted = True
        self._publish_state()

    def next_screen(self) -> None:
        """The clock's button: move on to the next rotation entry now (ends a pinned
        screen and a pause too). With rotation off this is the only way screens change."""
        self._pin = None
        self._paused = False
        self._pause_until = None
        self._reshow = None
        if not self.rotation_enabled:
            self._advance = True
        self._interrupted = True
        _LOG.info("Button pressed - next screen")

    def pause(self, seconds: float | None = None) -> None:
        """Hand the tubes back to the clock (display=0) until resume() or `seconds`
        pass (None = until resume). Pins and flashes still show during a pause."""
        self._paused = True
        self._pause_until = None if seconds is None else self._now() + seconds
        self._interrupted = True
        self._publish_state()
        _LOG.info("Paused%s", f" for {seconds:g}s" if seconds is not None else "")

    def resume(self) -> None:
        """End pause, pin and flash - back to the rotation."""
        self._paused = False
        self._pause_until = None
        self._pin = None
        self._flash = None
        self._reshow = None
        self._interrupted = True
        self._publish_state()
        _LOG.info("Resumed")

    def flash(self, message: str, seconds: float = 10, icon: str | None = None,
              color: str | None = None) -> None:
        """Show a message now for `seconds`, then return to exactly what was active
        before (the pinned screen if its time has not run out, the pause, or the
        rotation with the same entry re-shown). A new flash replaces a running one."""
        prev = {"pin": self._pin, "paused": self._paused, "pause_until": self._pause_until,
                "current": self.current}
        self._flash = {"message": message, "expiry": self._now() + seconds,
                       "icon": icon, "color": color, "prev": prev}
        self._interrupted = True
        self._publish_state()
        _LOG.info("Flash message %r for %gs", message, seconds)

    def _end_flash(self) -> None:
        f = self._flash
        if f is None:
            return
        self._flash = None
        prev = f["prev"]
        pin = prev.get("pin")
        if pin is not None and pin[1] is not None and self._now() >= pin[1]:
            pin = None    # the pinned screen's time ran out while the message showed
        self._pin = pin
        self._paused = bool(prev.get("paused"))
        self._pause_until = prev.get("pause_until")
        if pin is None and not self._paused:
            self._reshow = prev.get("current")   # re-show the same rotation entry
        self._interrupted = True
        self._publish_state()
        _LOG.info("Flash ended - back to %s",
                  pin[0] if pin else ("pause" if self._paused else self._reshow))

    # ------------------------------------------------------------------
    # live mirror (the editor's "Show on clock"): an in-memory screen shown
    # like a pinned one, with priority just below a flash
    # ------------------------------------------------------------------

    def mirror(self, screen: Screen, seconds: float = MIRROR_DEFAULT_SECONDS) -> None:
        """Show this in-memory screen (not from the screens file) like a pinned
        screen, with priority just below a flash. Each call replaces the screen and
        restarts the timer. The normal refresh keeps it up to date."""
        seconds = max(1.0, min(float(seconds), MIRROR_MAX_SECONDS))
        self._mirror = (screen, self._now() + seconds)
        self._interrupted = True
        self._publish_state()
        _LOG.info("Mirroring screen '%s' for %gs", screen.name, seconds)

    def end_mirror(self) -> None:
        """End the mirror; the rotation/pin/pause resumes."""
        if self._mirror is None:
            return
        self._mirror = None
        self._interrupted = True
        self._publish_state()
        _LOG.info("Mirror ended")

    def set_rotation_enabled(self, enabled: bool) -> None:
        self.rotation_enabled = enabled
        self._interrupted = True
        self._save_rotation_enabled()
        self._publish_state()       # keeps Home Assistant's Rotation switch in step
        _LOG.info("Rotation %s", "enabled" if enabled else "off - screens change by button press")

    # Rotation on/off survives restarts: kept in the app's /data folder, one entry
    # per clock ({"clocks": {"<name>": {"rotation": true}}}); the legacy single-clock
    # form {"rotation": x} is read as the "main" clock's entry.
    STATE_FILE = "/data/tube_display_state.json"

    def _load_rotation_enabled(self) -> bool:
        try:
            with open(self.STATE_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError, AttributeError):
            return True
        if not isinstance(data, dict):
            return True
        clocks = data.get("clocks")
        if isinstance(clocks, dict):
            entry = clocks.get(self.name)
            if isinstance(entry, dict) and "rotation" in entry:
                return bool(entry["rotation"])
        if self.name == "main" and "rotation" in data:
            return bool(data["rotation"])     # legacy {"rotation": x} form
        return True

    def _save_rotation_enabled(self) -> None:
        try:
            data: dict = {}
            try:
                with open(self.STATE_FILE, encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    data = loaded
            except (OSError, ValueError):
                pass
            clocks = data.get("clocks")
            if not isinstance(clocks, dict):
                clocks = {}
                data["clocks"] = clocks
            # read-modify-write: other clocks' entries are kept
            clocks[self.name] = {"rotation": bool(self.rotation_enabled)}
            with open(self.STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except OSError:
            pass    # no /data (tests, local runs): just not remembered

    def _is_paused(self) -> bool:
        if not self._paused:
            return False
        return self._pause_until is None or self._now() < self._pause_until

    def _ota_busy(self) -> bool:
        """OTA in progress and not given up on (10 min) yet."""
        return self._ota and (self._now() - self._ota_since) < OTA_TIMEOUT

    def _online(self) -> bool:
        """The clock answers MQTT and the pusher can reach it."""
        reachable = self._pusher is None or getattr(self._pusher, "reachable", True)
        return self._clock_online and reachable

    def _quiet_now(self) -> bool:
        cfg = self._config
        if cfg is None or cfg.night is None or cfg.night.quiet is None:
            return False
        q = cfg.night.quiet
        return night_mod.in_quiet(self.local_now().time(), q.start, q.end)

    async def _manual_step(self) -> None:
        """Rotation off + button: show the next screen in the rotation and stay there. The clock
        entry is skipped, since the button only reaches the app while panels show."""
        cfg = self._config
        rotation = self._rotation()
        if cfg is None or not rotation:
            return
        for _ in range(len(rotation)):
            self._rotation_index = (self._rotation_index + 1) % len(rotation)
            entry = rotation[self._rotation_index]
            screen = cfg.screens.get(entry) if entry != CLOCK_ENTRY else None
            if screen is None or not await render_mod.screen_when_true(screen, self._ctx):
                continue
            await self._activate_screen(screen)
            return

    def status(self) -> dict:
        cfg = self._config
        paused = self._is_paused()
        return {
            "current": self.current,
            "rotation": self._rotation(),
            "rotation_enabled": self.rotation_enabled,
            "pinned": self._pin[0] if self._pin else None,
            "screens": sorted(cfg.screens) if cfg else [],
            "config_errors": self.config_errors,
            "last_error": self.last_error or (self._pusher.last_error if self._pusher else None),
            "clock_state": self.clock_state,
            "clock_host": self.clock.host if self.clock is not None else self.settings.clock_host,
            "name": self.name,
            "paused": paused,
            "paused_until": (self._pause_until - self._now())
                            if paused and self._pause_until is not None else None,
            "message": self._flash["message"] if self._flash else None,
            "quiet": self._quiet_now(),
            "online": self._online(),
            "ota_busy": self._ota_busy(),
            "brightness": self._brightness,
            "live": bool(self.live),
            "mirroring": self._mirror is not None
                          and self._now() < self._mirror[1],
        }

    async def render_preview(self, name: str) -> Image.Image:
        if self._config is None or name not in self._config.screens:
            raise KeyError(name)
        return await render_mod.render_screen(self._config.screens[name], self._ctx)

    # ------------------------------------------------------------------
    # clock state from MQTT (health: availability, OTA)
    # ------------------------------------------------------------------

    async def on_clock_state(self, state: dict) -> None:
        """Called when the clock publishes its state on MQTT."""
        self.clock_state = state
        needed = state.get("panels_needed") or 0
        if isinstance(needed, str):
            try:
                needed = int(needed, 0)
            except ValueError:
                needed = 0
        if (needed and self.current not in (None, CLOCK_ENTRY) and self.last_strip is not None
                and self._pusher is not None and not self._ota_busy()
                and getattr(self._pusher, "reachable", True)):
            # picture was lost (e.g. after the screen saver): re-send immediately
            _LOG.info("Clock needs panels re-sent (mask 0x%x) - sending now", needed)
            tube_bytes = strip_to_tube_bytes(self.last_strip)
            await self._pusher.send_tubes(tube_bytes, needed_mask=needed)

    async def on_clock_availability(self, online: bool) -> None:
        """The clock's availability topic: offline stops pushing; back online resets
        the pusher and re-sends all six tubes if a screen/message is showing."""
        was = self._clock_online
        self._clock_online = online
        if not online:
            self._interrupted = True
            self._publish_state()
            _LOG.warning("Clock %s (%s) is offline", self.name, self.mqtt_id)
            return
        if not was:
            _LOG.info("Clock %s is back online - re-sending what is showing", self.name)
            if self._pusher is not None:
                self._pusher.reset()
            if self._ota:
                self._end_ota()     # availability back after an offline ends an OTA
            self._interrupted = True
            if (self._pusher is not None and not self._ota_busy() and self.last_strip is not None
                    and (self._flash is not None or self.current not in (None, CLOCK_ENTRY))):
                tube_bytes = strip_to_tube_bytes(self.last_strip)
                await self._pusher.send_tubes(tube_bytes, needed_mask=ALL_TUBES_MASK)
        self._publish_state()

    async def on_ota_state(self, state: dict) -> None:
        """The clock's OTA state: while an update runs nothing is sent and no display
        command is published (until it ends, the clock comes back online, or 10 min
        pass)."""
        in_progress = bool(state.get("in_progress"))
        if in_progress and not self._ota:
            self._ota = True
            self._ota_since = self._now()
            self._interrupted = True
            self._publish_state()
            _LOG.warning("OTA in progress on clock %s - sending nothing until it ends", self.name)
        elif not in_progress and self._ota:
            self._end_ota()

    def _end_ota(self) -> None:
        if not self._ota:
            return
        self._ota = False
        _LOG.info("OTA on clock %s finished - resuming", self.name)
        if self._pusher is not None:
            self._pusher.reset()
        self._reshow = self.current
        self._interrupted = True
        self._publish_state()

    # ------------------------------------------------------------------
    # reachability: probe with backoff instead of pushing into a dead clock
    # ------------------------------------------------------------------

    async def _ensure_reachable(self) -> bool:
        """While the pusher is unreachable, probe (GET /panel) with backoff
        5, 10, 20, 40, 60, 60... s instead of trying to push. Returns whether the
        pusher may be used now; on a successful probe everything is re-sent."""
        p = self._pusher
        if p is None or getattr(p, "reachable", True):
            return True
        now = self._now()
        if now < self._next_probe:
            return False
        ok = await p.probe()
        if ok:
            self._probe_failures = 0
            self._next_probe = 0.0
            p.reset()
            self._interrupted = True    # re-send everything on the next activation
            _LOG.info("Clock %s reachable again - re-sending", self.name)
        else:
            self._probe_failures += 1
            delay = PROBE_BACKOFF[min(self._probe_failures - 1, len(PROBE_BACKOFF) - 1)]
            self._next_probe = now + delay
            _LOG.debug("Clock %s still unreachable - next probe in %gs", self.name, delay)
        return ok

    # ------------------------------------------------------------------
    # night mode
    # ------------------------------------------------------------------

    async def _night_tick(self) -> None:
        """Night-mode bookkeeping, about every 60 s (by self._now, inside the main
        loop - no second task): sun elevation -> brightness, and the dim factor."""
        cfg = self._config
        if cfg is None or cfg.night is None:
            return
        now = self._now()
        if now - self._last_night_tick < NIGHT_TICK:
            return
        self._last_night_tick = now
        night = cfg.night
        if self._renderer is None:
            return
        try:
            raw = await self._renderer(SUN_ELEVATION_TEMPLATE)
            elevation = float(str(raw).strip())
        except Exception as err:
            _LOG.debug("Night mode: sun elevation unavailable (%s) - skipping", err)
            return
        self.daylight = night_mod.daylight(elevation, night.bright_above, night.dim_below)
        target = night_mod.brightness(self.daylight, night.brightness_day, night.brightness_night)
        if self._brightness is None or abs(target - self._brightness) >= 3:
            if self._mqtt is not None:
                await self._mqtt.publish_brightness(self.mqtt_id, target)
            self._brightness = target
            _LOG.info("Clock %s: elevation %.1f -> daylight %.2f, brightness %d",
                      self.name, elevation, self.daylight, target)

    def _dim(self, strip: Image.Image) -> Image.Image:
        """Multiply the strip's colours by the night dim factor (screens and
        messages only - the preview API is never dimmed)."""
        cfg = self._config
        if cfg is None or cfg.night is None:
            return strip
        f = night_mod.dim_factor(self.daylight, cfg.night.dim_screens)
        if f >= 0.999:
            return strip
        return Image.eval(strip, lambda v: int(v * f))

    # ------------------------------------------------------------------
    # showing entries
    # ------------------------------------------------------------------

    async def _publish_display(self, mode: int) -> None:
        if self._ota_busy():
            return      # during an OTA no display commands are published
        if self._mqtt is not None:
            await self._mqtt.publish_display(self.mqtt_id, mode)

    async def _activate_screen(self, screen: Screen, transition: str | None = None) -> None:
        """Publish display=4 if needed, render, and send all six tubes.

        `transition` (None = the screen's own resolved one) controls how the tubes
        appear when the screen BECOMES active: "none" sends 0..5 straight away,
        the others send tube by tube (or band by band for "wipe") with
        transition_delay ms between steps. An interrupt (button, pin, flash, ...)
        aborts a running transition."""
        if self._pusher is None or self._ota_busy():
            return      # during an OTA nothing is sent at all
        if not await self._ensure_reachable():
            return
        if self.clock_state.get("display") != DISPLAY_PANELS:
            await self._publish_display(DISPLAY_PANELS)
            await self._sleep(0.5)  # give the clock a moment to switch mode
        strip = self._dim(await render_mod.render_screen(screen, self._ctx))
        self.last_strip = strip
        self._screen_since = self._now()
        await self._cache_tubes(screen)
        tube_bytes = strip_to_tube_bytes(strip)
        cfg = self._config
        default_t = cfg.transition if cfg is not None else DEFAULT_TRANSITION
        t = transition if transition is not None else screen.resolved_transition(default_t)
        delay_ms = screen.resolved_transition_delay(DEFAULT_TRANSITION_DELAY)
        if t == "none" or self._interrupted or self._stop:
            await self._pusher.send_tubes(tube_bytes, needed_mask=ALL_TUBES_MASK)
        else:
            await self._run_transition(t, tube_bytes, delay_ms)
        self.current = screen.name
        self._publish_state()

    async def _run_transition(self, t: str, tube_bytes: list[bytes], delay_ms: int) -> None:
        """Send the tubes one by one (or band by band for a wipe), aborting as soon
        as an interrupt/stop/OTA/unreachable arrives. All sends still go through the
        pusher's lock, so only one upload runs at a time."""
        p = self._pusher
        if p is None:
            return
        self._transitioning = True
        try:
            delay = delay_ms / 1000.0

            def aborted() -> bool:
                return (self._interrupted or self._stop or self._ota_busy()
                        or not getattr(p, "reachable", True))

            if t == "wipe":
                for tube in range(TUBE_COUNT):          # tubes left to right
                    if aborted():
                        return
                    data = tube_bytes[tube]
                    for band in range(WIPE_BANDS):      # top to bottom in 6 bands
                        if aborted():
                            return
                        y = band * WIPE_BAND_H
                        h = WIPE_BAND_H if band < WIPE_BANDS - 1 else TUBE_H - y
                        ok = await p.send_rect(tube, 0, y, TUBE_W, h,
                                               data[y * TUBE_W * 2:(y + h) * TUBE_W * 2])
                        if not ok:
                            return
                        if band < WIPE_BANDS - 1 and delay > 0 and not aborted():
                            await self._sleep(delay)
            else:
                order = TRANSITION_ORDERS.get(t, TRANSITION_ORDERS["cascade"])
                for i, tube in enumerate(order):
                    if aborted():
                        return
                    ok = await p.send_tube(tube, tube_bytes[tube])
                    if not ok:
                        return
                    if i < len(order) - 1 and delay > 0 and not aborted():
                        await self._sleep(delay)
        finally:
            self._transitioning = False

    async def _cache_tubes(self, screen: Screen) -> None:
        """Re-resolve the showing screen's six tube widgets (templates resolved the
        same way render_screen does it) so animators can look at their types."""
        self._tubes = await render_mod.resolved_tubes(screen, self._ctx)
        # the screen's background, for animators whose regions show it (e.g. the ticker band);
        # a templated background is left to the animator's own default
        self._anim_bg = render_mod.parse_color(screen.background, (0, 0, 0)) \
            if isinstance(screen.background, str) and "{" not in screen.background else None
        self._anim_next = {}
        now = self._now()
        for i, w in enumerate(self._tubes or []):
            if w is None:
                continue
            animator = animate_mod.ANIMATORS.get(str(w.get("type")))
            if animator is not None:
                interval = animator.interval(w)
                if interval and interval > 0:
                    self._anim_next[i] = now + interval

    def _anim_blocked(self) -> bool:
        """Nothing animates while OTA is busy, the pusher is unreachable, paused,
        quiet, a flash shows, or during a transition."""
        return (self._ota_busy() or self._transitioning or self._flash is not None
                or self._is_paused() or self._quiet_now()
                or self._pusher is None or not getattr(self._pusher, "reachable", True))

    async def _anim_tick(self) -> None:
        """Render and send the next due frame of every animated tube (send_rect).
        Frames are skipped (not queued) when late; at most ANIM_MAX_RECTS_PER_SEC
        rect uploads per second in total."""
        if self._anim_blocked() or not self._anim_next or self._tubes is None:
            return
        now = self._now()
        # rate cap: drop rect timestamps older than 1 s
        self._anim_rect_times = [t for t in self._anim_rect_times if now - t < 1.0]
        if len(self._anim_rect_times) >= ANIM_MAX_RECTS_PER_SEC:
            return
        t = now - self._screen_since
        for tube, due in sorted(self._anim_next.items()):
            if now < due:
                continue
            w = self._tubes[tube] if tube < len(self._tubes) else None
            if w is None:
                continue
            animator = animate_mod.ANIMATORS.get(str(w.get("type")))
            if animator is None:
                continue
            interval = animator.interval(w)
            if not interval or interval <= 0:
                continue
            # skip (not queue) when late: the next frame is due one interval after now
            self._anim_next[tube] = now + interval
            try:
                result = await animator.frame(w, self._ctx, t, getattr(self, "_anim_bg", None))
            except Exception as err:
                _LOG.debug("Animator for tube %d failed: %s", tube, err)
                continue
            if result is None:
                continue
            img, (x, y, w_, h_) = result
            if img is None or w_ <= 0 or h_ <= 0:
                continue
            if img.size != (w_, h_):
                img = img.resize((w_, h_))
            img = self._dim(img)
            if len(self._anim_rect_times) >= ANIM_MAX_RECTS_PER_SEC:
                break
            ok = await self._pusher.send_rect(tube, x, y, w_, h_, image_to_rgb565(img))
            self._anim_rect_times.append(self._now())
            if not ok:
                break

    async def _activate_clock(self) -> None:
        await self._publish_display(DISPLAY_CLOCK)
        self.current = CLOCK_ENTRY
        self.last_strip = None
        self._tubes = None
        self._anim_next = {}
        self._publish_state()

    async def _show_flash(self) -> None:
        """Render and send the flash message once (all six tubes), with the global
        default transition."""
        f = self._flash
        if f is None or self._pusher is None or self._ota_busy():
            return
        if not await self._ensure_reachable():
            return
        if self.clock_state.get("display") != DISPLAY_PANELS:
            await self._publish_display(DISPLAY_PANELS)
            await self._sleep(0.5)
        strip = self._dim(render_mod.render_message(f["message"], self._ctx,
                                                    icon=f.get("icon"), color=f.get("color")))
        self.last_strip = strip
        self._tubes = None          # a message has no per-tube widgets to animate
        self._anim_next = {}
        tube_bytes = strip_to_tube_bytes(strip)
        cfg = self._config
        t = cfg.transition if cfg is not None else DEFAULT_TRANSITION
        if t == "none" or self._interrupted or self._stop:
            await self._pusher.send_tubes(tube_bytes, needed_mask=ALL_TUBES_MASK)
        else:
            await self._run_transition(t, tube_bytes, DEFAULT_TRANSITION_DELAY)
        self._publish_state()

    async def _show_paused(self) -> None:
        """Pause: hand the tubes back to the clock once, then just wait."""
        if not self._pause_shown:
            self._pause_shown = True
            await self._publish_display(DISPLAY_CLOCK)
            self.current = CLOCK_ENTRY
            self.last_strip = None
            self._publish_state()
        self._interrupted = False
        while self._is_paused() and not self._interrupted and not self._stop:
            await self._sleep(0.25)
            await self._night_tick()      # brightness follows the sun even while paused
        if not self._is_paused():
            self._paused = False
            self._pause_until = None
        self._pause_shown = False

    async def _show_quiet(self) -> None:
        """Quiet hours: replace the rotation by the clock's own display (mode
        "clock") or an all-black strip (mode "blank"), pushed once."""
        cfg = self._config
        mode = "clock"
        if cfg is not None and cfg.night is not None and cfg.night.quiet is not None:
            mode = cfg.night.quiet.mode
        if not self._quiet_shown:
            self._quiet_shown = True
            if mode == "clock":
                await self._activate_clock()     # publish_display is OTA-guarded
            elif (self._pusher is not None and not self._ota_busy()
                    and await self._ensure_reachable()):
                if self.clock_state.get("display") != DISPLAY_PANELS:
                    await self._publish_display(DISPLAY_PANELS)
                    await self._sleep(0.5)
                strip = Image.new("RGB", (STRIP_W, STRIP_H), (0, 0, 0))
                self.last_strip = strip
                await self._pusher.send_tubes(strip_to_tube_bytes(strip), needed_mask=ALL_TUBES_MASK)
                self._publish_state()
        self._interrupted = False
        while self._quiet_now() and not self._interrupted and not self._stop:
            await self._sleep(0.25)
            await self._night_tick()      # brightness follows the sun during quiet hours
        self._quiet_shown = False

    async def _refresh(self) -> None:
        """Re-render the current screen and push changed/needed tubes."""
        if (self._pusher is None or self._config is None or self._ota_busy()
                or self.current in (None, CLOCK_ENTRY)):
            return
        if not await self._ensure_reachable():
            return
        screen = self._mirror[0] if self._mirror_active() else self._config.screens.get(self.current)
        if screen is None:
            return
        strip = self._dim(await render_mod.render_screen(screen, self._ctx))
        self.last_strip = strip
        await self._cache_tubes(screen)      # re-resolve the widgets after every full render
        needed = self.clock_state.get("panels_needed") or 0
        if isinstance(needed, str):
            try:
                needed = int(needed, 0)
            except ValueError:
                needed = 0
        sent = await self._pusher.send_tubes(strip_to_tube_bytes(strip), needed_mask=needed)
        if sent:
            _LOG.debug("Refreshed screen '%s', sent tubes %s", self.current, sent)

    def _mirror_active(self) -> bool:
        return self._mirror is not None and self._now() < self._mirror[1]

    def _publish_state(self) -> None:
        if self._mqtt is None:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return      # called outside the app's event loop (tests): nothing to publish on
        asyncio.ensure_future(self._mqtt.publish_state(self))

    def _mqtt_state(self) -> dict:
        cfg = self._config
        options = ["Rotate", "Clock"] + (sorted(cfg.screens) if cfg else [])
        if self._pin is not None:
            select_value = self._pin[0] if self._pin[0] != CLOCK_ENTRY else "Clock"
        elif not self.rotation_enabled:
            select_value = self.current if self.current and self.current != CLOCK_ENTRY else "Rotate"
        else:
            select_value = "Rotate"
        return {
            "name": self.name,
            "screen": select_value,
            "screen_options": options,
            "rotation": "on" if self.rotation_enabled else "off",
        }

    def on_template_change(self) -> None:
        """Live updates hook (sync, called by the template-cache module whenever any
        Home Assistant template result changes): the waiting loops notice the dirty
        flag within 0.25 s and re-render, at most once per `live_min_interval`."""
        self._live_dirty = True

    async def _live_refresh(self, next_refresh: float, refresh: float) -> float:
        """If a template changed and the throttle allows it, re-render now.
        Returns the new next_refresh."""
        if self._live_dirty and self._now() - self._last_live >= self.live_min_interval:
            self._live_dirty = False
            self._last_live = self._now()
            await self._refresh()
            return self._now() + refresh
        return next_refresh

    async def _show_screen(self, screen: Screen, duration: float) -> None:
        # Counted from the start, so the time spent switching the clock's mode and sending the
        # first picture is part of the screen's duration
        end = self._now() + duration
        await self._activate_screen(screen)
        refresh = screen.resolved_refresh(self.settings.default_refresh)
        next_refresh = self._now() + refresh
        while True:
            now = self._now()
            if now >= end or self._interrupted:
                break
            # In short steps so a button press, a pin or a live update takes effect
            # within a quarter second
            wait = min(next_refresh, end, now + 0.25) - now
            if wait > 0:
                await self._sleep(wait)
            if self._interrupted:
                break
            await self._night_tick()      # brightness follows the sun while a screen shows
            await self._anim_tick()       # animated tubes send their next frame when due
            next_refresh = await self._live_refresh(next_refresh, refresh)
            if self._now() >= next_refresh - 1e-9:
                await self._refresh()
                next_refresh = self._now() + refresh

    async def _show_clock(self, duration: float) -> None:
        await self._activate_clock()
        end = self._now() + duration
        while self._now() < end and not self._interrupted:
            await self._sleep(min(1.0, end - self._now()))
            await self._night_tick()      # brightness follows the sun while the clock shows

    async def _show_entry(self, entry: str) -> None:
        """Show one rotation entry once (used to re-show the entry that was active
        before a flash/OTA, without advancing the rotation)."""
        cfg = self._config
        if cfg is None:
            return
        rotation = self._rotation()
        if entry in rotation:
            self._rotation_index = rotation.index(entry)
        self._interrupted = False
        if entry == CLOCK_ENTRY:
            await self._show_clock(self._clock_duration())
        else:
            screen = cfg.screens.get(entry)
            if screen is not None:
                await self._show_screen(screen, screen.resolved_duration(self.settings.default_duration))

    async def run_next_entry(self) -> str:
        """Advance the rotation by one entry (skipping when-false screens)."""
        cfg = self._config
        rotation = self._rotation()
        if cfg is None or not rotation:
            await self._sleep(1.0)
            return self.current or ""
        self._interrupted = False
        for _ in range(len(rotation) + 1):
            self._rotation_index = (self._rotation_index + 1) % len(rotation)
            entry = rotation[self._rotation_index]
            if entry == CLOCK_ENTRY:
                await self._show_clock(self._clock_duration())
                return entry
            screen = cfg.screens.get(entry)
            if screen is None:
                continue
            if not await render_mod.screen_when_true(screen, self._ctx):
                _LOG.debug("Skipping screen '%s' (when is not true)", entry)
                continue
            await self._show_screen(screen, screen.resolved_duration(self.settings.default_duration))
            return entry
        # every entry was skipped
        await self._sleep(1.0)
        return self.current or ""

    # ------------------------------------------------------------------
    # main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        self.load_config(force=True)
        while not self._stop:
            try:
                await self._reload_if_changed()
                await self._night_tick()
                await self._ensure_reachable()
                if self._ota:
                    if self._ota_busy():
                        await self._sleep(1.0)   # OTA: send nothing at all
                        continue
                    self._end_ota()               # 10 minutes passed
                if self._flash is not None:
                    if self._now() >= self._flash["expiry"]:
                        self._end_flash()
                        continue
                    self._interrupted = False
                    await self._show_flash()
                    while (self._flash is not None and not self._interrupted
                           and self._now() < self._flash["expiry"]):
                        await self._sleep(min(0.25, self._flash["expiry"] - self._now()))
                        await self._night_tick()   # brightness follows the sun during a flash
                    if self._flash is not None and self._now() >= self._flash["expiry"]:
                        self._end_flash()
                    continue
                if self._mirror is not None:
                    screen, expiry = self._mirror
                    if self._now() >= expiry:
                        _LOG.info("Mirror expired - resuming")
                        self._mirror = None
                        continue
                    self._interrupted = False
                    await self._show_screen(screen, expiry - self._now())
                    continue
                if self._pin is not None:
                    entry, expiry = self._pin
                    if expiry is not None and self._now() >= expiry:
                        _LOG.info("Pinned screen '%s' expired - resuming rotation", entry)
                        self._pin = None
                        continue
                    duration = (expiry - self._now()) if expiry is not None else self._pin_duration(entry)
                    self._interrupted = False
                    if entry == CLOCK_ENTRY:
                        await self._show_clock(duration)
                    else:
                        screen = self._config.screens.get(entry) if self._config else None
                        if screen is not None:
                            await self._show_screen(screen, duration)
                    continue
                if self._is_paused():
                    await self._show_paused()
                    continue
                if self._quiet_now():
                    await self._show_quiet()
                    continue
                if not self.rotation_enabled:
                    # Off: stay on the current screen, keeping its values up to date, until the
                    # clock's button asks for the next one
                    if self._reshow is not None:
                        # a flash/OTA just ended: re-show that entry now instead of waiting
                        # for the next refresh
                        entry = self._reshow
                        self._reshow = None
                        screen = (self._config.screens.get(entry)
                                  if self._config and entry != CLOCK_ENTRY else None)
                        if screen is not None:
                            await self._activate_screen(screen)
                            continue
                        if entry == CLOCK_ENTRY:
                            await self._activate_clock()
                            continue
                    screen = (self._config.screens.get(self.current)
                              if self._config and self.current not in (None, CLOCK_ENTRY) else None)
                    if self._advance or (screen is None and self.current is None):
                        self._advance = False
                        await self._manual_step()
                        continue
                    if screen is None:
                        await self._sleep(1.0)
                        continue
                    self._interrupted = False
                    refresh = screen.resolved_refresh(self.settings.default_refresh)
                    end = self._now() + refresh
                    while self._now() < end and not self._interrupted:
                        await self._sleep(min(0.25, end - self._now()))
                        await self._night_tick()   # brightness follows the sun with rotation off
                        await self._anim_tick()    # animated tubes keep moving here too
                        if self._now() < end and not self._interrupted:
                            end = await self._live_refresh(end, refresh)
                    if not self._interrupted:
                        await self._refresh()
                    continue
                if self._reshow is not None:
                    entry = self._reshow
                    self._reshow = None
                    await self._show_entry(entry)
                    continue
                await self.run_next_entry()
            except asyncio.CancelledError:
                raise
            except Exception as err:
                self.last_error = str(err)
                _LOG.exception("Rotation loop error: %s", err)
                await self._sleep(2.0)

    def _pin_duration(self, entry: str) -> float:
        cfg = self._config
        if entry == CLOCK_ENTRY:
            return self._clock_duration()
        screen = cfg.screens.get(entry) if cfg else None
        if screen is not None:
            return screen.resolved_duration(self.settings.default_duration)
        return self.settings.default_duration

    def stop(self) -> None:
        self._stop = True
        self._interrupted = True   # wake any waiting loop so run() can exit now
