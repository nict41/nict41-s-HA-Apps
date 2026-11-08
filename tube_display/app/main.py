"""Tube Display entry point.

Wires everything together: settings, one aiohttp session, the HA client, the
clock pusher, the MQTT bridge, the rotation controller and the HTTP API on
0.0.0.0:8098. Writes a commented example /config/screens.yaml on first start
if the file is missing.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import aiohttp
from aiohttp import web

import api as api_mod
import editor as editor_mod
import mqtt_bridge
from clock import ClockPusher
from controller import Controller
from ha import HaClient
from screens import example_screens_yaml
from settings import API_PORT, load_settings

_LOG = logging.getLogger("tube_display")


def ensure_screens_file(path: str) -> None:
    """Write the commented example screens.yaml on first start if missing."""
    old = "/config/screens.yaml"  # where 0.1.0 kept it, in the app's own config folder
    if not os.path.exists(path) and path != old and os.path.exists(old):
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(old, encoding="utf-8") as src, open(path, "w", encoding="utf-8") as dst:
                dst.write(src.read())
            _LOG.info("Copied %s to %s - edit the new one from now on.", old, path)
            return
        except OSError as err:
            _LOG.error("Could not copy %s to %s: %s", old, path, err)
    if not os.path.exists(path):
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(example_screens_yaml())
            _LOG.info("Wrote example screens file to %s - edit it and the rotation picks it up.", path)
        except OSError as err:
            _LOG.error("Could not write example screens file %s: %s", path, err)


async def run() -> None:
    settings = load_settings()
    logging.basicConfig(
        level=settings.logging_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    _LOG.info("Tube Display starting (clock %s, mqtt id %s, screens %s)",
              settings.clock_host, settings.clock_mqtt_id, settings.screens_file)
    ensure_screens_file(settings.screens_file)

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        token = os.environ.get("SUPERVISOR_TOKEN", "")
        ha = HaClient(session, token=token)

        mqtt: mqtt_bridge.MqttBridge | None = None

        async def on_clock_state(state: dict) -> None:
            await controller.on_clock_state(state)

        async def on_select(value: str) -> None:
            if value == mqtt_bridge.ROTATE:
                controller.clear_pin()
            elif value == mqtt_bridge.CLOCK:
                controller.show_screen("clock")
            else:
                try:
                    controller.show_screen(value)
                except KeyError:
                    _LOG.warning("MQTT select: unknown screen %r", value)

        async def on_rotation(on: bool) -> None:
            controller.set_rotation_enabled(on)

        async def on_button() -> None:
            controller.next_screen()

        controller = Controller(settings, session, ha)
        pusher = ClockPusher(
            session, settings.clock_base_url,
            user=settings.clock_user, password=settings.clock_password,
        )
        controller.set_pusher(pusher)

        if settings.mqtt:
            mqtt = mqtt_bridge.MqttBridge(
                settings.mqtt, settings.clock_mqtt_id,
                on_clock_state, on_select, on_rotation,
                get_state=controller._mqtt_state,
                on_button=on_button,
            )
            pusher._publish_display = mqtt.publish_display
            controller.set_mqtt(mqtt)
            await mqtt.start()

        app = api_mod.create_app(controller)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", API_PORT)
        await site.start()
        _LOG.info("HTTP API listening on 0.0.0.0:%d", API_PORT)

        # The screen editor for the HA ingress panel: a second site on 8099
        # (not published in ports:), only reachable from the ingress proxy.
        editor_app = editor_mod.create_app(controller, ha, settings.screens_file)
        editor_runner = web.AppRunner(editor_app)
        await editor_runner.setup()
        editor_site = web.TCPSite(editor_runner, "0.0.0.0", editor_mod.INGRESS_PORT)
        await editor_site.start()
        _LOG.info("Screen editor (ingress) listening on 0.0.0.0:%d", editor_mod.INGRESS_PORT)

        rotation_task = asyncio.ensure_future(controller.run())
        try:
            await rotation_task
        except asyncio.CancelledError:
            pass
        finally:
            controller.stop()
            if mqtt is not None:
                await mqtt.stop()
            await runner.cleanup()
            await editor_runner.cleanup()


def main() -> int:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
