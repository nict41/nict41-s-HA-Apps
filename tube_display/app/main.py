"""Tube Display entry point.

Wires everything together: settings, one aiohttp session, the HA client, one
clock pusher and one controller per configured clock, one MQTT bridge for all
of them, and the HTTP API on 0.0.0.0:8098. Writes a commented example
/config/screens.yaml on first start if the file is missing.

Also wired here: the websocket template cache (live updates, only with a
SUPERVISOR_TOKEN), the firmware publisher (MQTT OTA offers), and the bundled
integration install/announce. With no clocks configured the app still runs the
HTTP API and the editor - it just waits forever instead of exiting.
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
import firmware as firmware_mod
import ha_ws
import integration_installer
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
    for err in settings.errors:
        _LOG.error("Settings: %s", err)
    _LOG.info("Tube Display starting (%d clock(s): %s, screens %s)",
              len(settings.clocks),
              ", ".join(f"{c.name}={c.host}" for c in settings.clocks) or "none",
              settings.screens_file)
    ensure_screens_file(settings.screens_file)

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        token = os.environ.get("SUPERVISOR_TOKEN", "")
        ha = HaClient(session, token=token)

        # Template cache (live updates): one websocket subscription per template,
        # shared by every controller. Only with a Supervisor token; without one
        # the controllers render over REST as before.
        cache: ha_ws.TemplateCache | None = None
        if token:
            def _on_template_change(template: str) -> None:
                for c in controllers:
                    c.on_template_change()

            cache = ha_ws.TemplateCache(session, token,
                                        rest_renderer=ha.render_template,
                                        on_change=_on_template_change)
            await cache.start()

        # One controller + pusher per clock, all sharing the session and HA client
        controllers: list[Controller] = []
        for clock in settings.clocks:
            controller = Controller(settings, session, ha, clock=clock,
                                    renderer=cache.render if cache is not None else None)
            if cache is not None:
                controller.live = True
            pusher = ClockPusher(session, clock.base_url,
                                 user=clock.user, password=clock.password)
            controller.set_pusher(pusher)
            controllers.append(controller)
        if not controllers:
            _LOG.warning("No clocks configured (TUBE_CLOCK_HOST / TUBE_CLOCKS_JSON) - "
                         "running the HTTP API and editor only")

        mqtt: mqtt_bridge.MqttBridge | None = None
        if settings.mqtt:
            mqtt = mqtt_bridge.MqttBridge(settings.mqtt, controllers)
            for controller in controllers:
                pusher = controller._pusher
                if pusher is not None:
                    # display-mode commands go over MQTT to that clock
                    pusher._publish_display = (
                        lambda mode, cid=controller.clock.mqtt_id:
                        mqtt.publish_display(cid, mode)
                    )
                controller.set_mqtt(mqtt)
            await mqtt.start()

        # Firmware publisher: offers HA Tubes firmware updates to the clocks over
        # MQTT (retained clock/<id>/ota/latest) and serves the .bin over HTTP.
        publisher: firmware_mod.FirmwarePublisher | None = None
        firmware_task: asyncio.Task | None = None
        if settings.firmware_updates and mqtt is not None:
            async def _base_url() -> str | None:
                if settings.app_url:
                    return settings.app_url.rstrip("/")
                return await firmware_mod.host_base_url(session, token, API_PORT)

            publisher = firmware_mod.FirmwarePublisher(
                session, "/data/firmware",
                publish=lambda topic, payload: mqtt.publish(topic, payload, retain=True),
                clock_ids=lambda: [c.clock.mqtt_id for c in controllers],
                base_url=_base_url,
                variant=settings.firmware_variant,
            )

            async def _firmware_run() -> None:
                # wait until MQTT is connected before the first publish (no timeout)
                await mqtt.connected.wait()
                await publisher.run()

            firmware_task = asyncio.ensure_future(_firmware_run())

        # Bundled integration: install it into HA's config folder (a restart
        # notification says so) and announce the app to the Supervisor. Never
        # fail start-up because of these.
        try:
            version, was_installed = integration_installer.install()
            if version:
                await integration_installer.notify_restart(session, token, version,
                                                           was_installed=was_installed)
            if token:
                await integration_installer.announce(session, token, API_PORT)
        except Exception as err:
            _LOG.warning("Integration install/announce failed (ignored): %s", err)

        def _extra_status() -> dict:
            return {
                "templates": cache.stats() if cache is not None else None,
                "firmware": ({"latest": publisher.latest, "last_error": publisher.last_error}
                             if publisher is not None else None),
            }

        app = api_mod.create_app(controllers, firmware=publisher, extra_status=_extra_status)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", API_PORT)
        await site.start()
        _LOG.info("HTTP API listening on 0.0.0.0:%d", API_PORT)

        # The screen editor for the HA ingress panel: a second site on 8099
        # (not published in ports:), only reachable from the ingress proxy.
        editor_app = editor_mod.create_app(
            controllers[0] if controllers else None, ha, settings.screens_file,
            controllers={c.name: c for c in controllers})
        editor_runner = web.AppRunner(editor_app)
        await editor_runner.setup()
        editor_site = web.TCPSite(editor_runner, "0.0.0.0", editor_mod.INGRESS_PORT)
        await editor_site.start()
        _LOG.info("Screen editor (ingress) listening on 0.0.0.0:%d", editor_mod.INGRESS_PORT)

        rotation_tasks = [asyncio.ensure_future(c.run()) for c in controllers]
        try:
            if rotation_tasks:
                await asyncio.gather(*rotation_tasks)
            else:
                # No clocks: keep the HTTP API + editor running forever
                _LOG.info("No clocks to drive - waiting (HTTP API and editor stay up)")
                await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass
        finally:
            for c in controllers:
                c.stop()
            if firmware_task is not None:
                firmware_task.cancel()
                try:
                    await firmware_task
                except (asyncio.CancelledError, Exception):
                    pass
            if cache is not None:
                await cache.stop()
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
