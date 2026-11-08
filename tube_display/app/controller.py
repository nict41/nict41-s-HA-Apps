"""The controller: rotation scheduler, screen activation, interrupts.

Time and sleeping are injectable (`now`, `sleep`) so tests can drive the
rotation with a fake clock. The loop:

- re-reads screens.yaml when its mtime changes,
- honours a pinned screen (POST /api/show, MQTT select) until it expires,
- otherwise cycles through `rotation`, skipping screens whose `when` is not
  "true", showing each for its duration and re-rendering every `refresh`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Awaitable, Callable

from PIL import Image

import render as render_mod
from clock import DISPLAY_CLOCK, DISPLAY_PANELS, ClockPusher
from rgb565 import STRIP_H, STRIP_W, strip_to_tube_bytes
from screens import CLOCK_ENTRY, Screen, ScreensConfig, ScreensError, load_screens

_LOG = logging.getLogger(__name__)

ALL_TUBES_MASK = 0b111111


class Controller:
    def __init__(self, settings, session, ha, pusher: ClockPusher | None = None, mqtt=None,
                 renderer: Callable[[str], Awaitable[str]] | None = None,
                 now: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
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

        self._config: ScreensConfig | None = None
        self._mtime: float | None = None
        self.current: str | None = None          # entry shown now ("clock" or a screen name)
        self.rotation_enabled = True
        self._pin: tuple[str, float | None] | None = None   # (entry, expiry; None = forever)
        self._rotation_index = -1
        self._interrupted = False
        self._stop = False
        self.clock_state: dict = {}               # last MQTT state from the clock
        self.last_error: str | None = None
        self.last_strip: Image.Image | None = None
        self.config_errors: list[str] = []
        self.screens_loaded_at: float | None = None

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

    def _clock_duration(self) -> int:
        cfg = self._config
        if cfg and cfg.clock_duration is not None:
            return cfg.clock_duration
        return self.settings.default_duration

    # ------------------------------------------------------------------
    # control API (called from HTTP API / MQTT)
    # ------------------------------------------------------------------

    def show_screen(self, name: str, seconds: float | None = None) -> None:
        """Interrupt the rotation with `name` for `seconds` (None = until changed)."""
        if name != CLOCK_ENTRY and self._config is not None and name not in self._config.screens:
            raise KeyError(name)
        expiry = None if seconds is None else self._now() + seconds
        self._pin = (name, expiry)
        self._interrupted = True
        _LOG.info("Pinned screen '%s'%s", name,
                  f" for {seconds:g}s" if seconds is not None else "")

    def clear_pin(self) -> None:
        self._pin = None
        self._interrupted = True

    def next_screen(self) -> None:
        """The clock's button: move on to the next rotation entry now (ends a pinned screen too)."""
        self._pin = None
        self._interrupted = True
        _LOG.info("Button pressed - next screen")

    def set_rotation_enabled(self, enabled: bool) -> None:
        self.rotation_enabled = enabled
        self._interrupted = True
        _LOG.info("Rotation %s", "enabled" if enabled else "paused")

    def status(self) -> dict:
        cfg = self._config
        return {
            "current": self.current,
            "rotation": list(cfg.rotation) if cfg else [],
            "rotation_enabled": self.rotation_enabled,
            "pinned": self._pin[0] if self._pin else None,
            "screens": sorted(cfg.screens) if cfg else [],
            "config_errors": self.config_errors,
            "last_error": self.last_error or (self._pusher.last_error if self._pusher else None),
            "clock_state": self.clock_state,
            "clock_host": self.settings.clock_host,
        }

    async def render_preview(self, name: str) -> Image.Image:
        if self._config is None or name not in self._config.screens:
            raise KeyError(name)
        return await render_mod.render_screen(self._config.screens[name], self._ctx)

    # ------------------------------------------------------------------
    # clock state from MQTT
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
        if needed and self.current not in (None, CLOCK_ENTRY) and self.last_strip is not None and self._pusher is not None:
            # picture was lost (e.g. after the screen saver): re-send immediately
            _LOG.info("Clock needs panels re-sent (mask 0x%x) - sending now", needed)
            tube_bytes = strip_to_tube_bytes(self.last_strip)
            await self._pusher.send_tubes(tube_bytes, needed_mask=needed)

    # ------------------------------------------------------------------
    # showing entries
    # ------------------------------------------------------------------

    async def _publish_display(self, mode: int) -> None:
        if self._mqtt is not None:
            await self._mqtt.publish_display(mode)

    async def _activate_screen(self, screen: Screen) -> None:
        """Publish display=4 if needed, render, and send all six tubes."""
        if self._pusher is None:
            return
        if self.clock_state.get("display") != DISPLAY_PANELS:
            await self._publish_display(DISPLAY_PANELS)
            await self._sleep(0.5)  # give the clock a moment to switch mode
        strip = await render_mod.render_screen(screen, self._ctx)
        self.last_strip = strip
        tube_bytes = strip_to_tube_bytes(strip)
        await self._pusher.send_tubes(tube_bytes, needed_mask=ALL_TUBES_MASK)
        self.current = screen.name
        self._publish_state()

    async def _activate_clock(self) -> None:
        await self._publish_display(DISPLAY_CLOCK)
        self.current = CLOCK_ENTRY
        self.last_strip = None
        self._publish_state()

    async def _refresh(self) -> None:
        """Re-render the current screen and push changed/needed tubes."""
        if self._pusher is None or self._config is None or self.current in (None, CLOCK_ENTRY):
            return
        screen = self._config.screens.get(self.current)
        if screen is None:
            return
        strip = await render_mod.render_screen(screen, self._ctx)
        self.last_strip = strip
        needed = self.clock_state.get("panels_needed") or 0
        if isinstance(needed, str):
            try:
                needed = int(needed, 0)
            except ValueError:
                needed = 0
        sent = await self._pusher.send_tubes(strip_to_tube_bytes(strip), needed_mask=needed)
        if sent:
            _LOG.debug("Refreshed screen '%s', sent tubes %s", self.current, sent)

    def _publish_state(self) -> None:
        if self._mqtt is not None:
            asyncio.ensure_future(self._mqtt.publish_state(self._mqtt_state()))

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
            "screen": select_value,
            "screen_options": options,
            "rotation": "on" if self.rotation_enabled else "off",
        }

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
            # In short steps so a button press or a pin takes effect within a quarter second
            wait = min(next_refresh, end, now + 0.25) - now
            if wait > 0:
                await self._sleep(wait)
            if self._interrupted:
                break
            if self._now() >= next_refresh - 1e-9:
                await self._refresh()
                next_refresh = self._now() + refresh

    async def _show_clock(self, duration: float) -> None:
        await self._activate_clock()
        end = self._now() + duration
        while self._now() < end and not self._interrupted:
            await self._sleep(min(1.0, end - self._now()))

    async def run_next_entry(self) -> str:
        """Advance the rotation by one entry (skipping when-false screens)."""
        cfg = self._config
        if cfg is None or not cfg.rotation:
            await self._sleep(1.0)
            return self.current or ""
        self._interrupted = False
        for _ in range(len(cfg.rotation) + 1):
            self._rotation_index = (self._rotation_index + 1) % len(cfg.rotation)
            entry = cfg.rotation[self._rotation_index]
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
                if not self.rotation_enabled:
                    # Paused: stay on the current screen, but keep its values up to date
                    screen = (self._config.screens.get(self.current)
                              if self._config and self.current not in (None, CLOCK_ENTRY) else None)
                    if screen is None:
                        await self._sleep(1.0)
                        continue
                    self._interrupted = False
                    refresh = screen.resolved_refresh(self.settings.default_refresh)
                    end = self._now() + refresh
                    while self._now() < end and not self._interrupted:
                        await self._sleep(min(1.0, end - self._now()))
                    if not self._interrupted:
                        await self._refresh()
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
