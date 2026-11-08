"""MQTT bridge: clock state subscription, display commands, HA discovery.

- Subscribes to clock/<id>/volatile/state (display, panel_tubes,
  panels_needed) and forwards it to the controller.
- Publishes display mode commands to clock/<id>/set/display.
- Publishes Home Assistant MQTT discovery for the device "Tube Display":
  a select "Tube Display Screen" (Rotate / Clock / every screen name) and a
  switch "Tube Display Rotation". Selecting a screen pins it until "Rotate"
  is chosen again.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable

import aiomqtt

_LOG = logging.getLogger(__name__)

BASE_TOPIC = "tube_display"
STATE_TOPIC = f"{BASE_TOPIC}/state"
AVAILABILITY_TOPIC = f"{BASE_TOPIC}/availability"
SCREEN_SELECT_STATE = f"{BASE_TOPIC}/screen/state"
SCREEN_SELECT_COMMAND = f"{BASE_TOPIC}/screen/set"
ROTATION_SWITCH_STATE = f"{BASE_TOPIC}/rotation/state"
ROTATION_SWITCH_COMMAND = f"{BASE_TOPIC}/rotation/set"

ROTATE = "Rotate"
CLOCK = "Clock"

_DEVICE = {
    "identifiers": [BASE_TOPIC],
    "name": "Tube Display",
    "manufacturer": "nict41's HA Apps",
    "model": "Tube Display",
}


class MqttBridge:
    def __init__(self, mqtt_cfg: dict, clock_mqtt_id: str,
                 on_clock_state: Callable[[dict], Awaitable[None]],
                 on_select: Callable[[str], Awaitable[None]],
                 on_rotation: Callable[[bool], Awaitable[None]],
                 get_state: Callable[[], dict],
                 on_button: Callable[[], Awaitable[None]] | None = None) -> None:
        self._cfg = mqtt_cfg
        self._clock_id = clock_mqtt_id
        self._on_clock_state = on_clock_state
        self._on_select = on_select
        self._on_rotation = on_rotation
        self._get_state = get_state
        self._on_button = on_button
        self._client: aiomqtt.Client | None = None
        self._task: asyncio.Task | None = None
        self._stop = False

    @property
    def enabled(self) -> bool:
        return bool(self._cfg.get("host"))

    @property
    def clock_state_topic(self) -> str:
        return f"clock/{self._clock_id}/volatile/state"

    @property
    def clock_button_topic(self) -> str:
        """The clock's button, pressed while panels show (firmware 2.2.0-nt3+)."""
        return f"clock/{self._clock_id}/button"

    @property
    def clock_display_command_topic(self) -> str:
        return f"clock/{self._clock_id}/set/display"

    async def start(self) -> None:
        if not self.enabled:
            _LOG.warning("No MQTT broker configured - clock state and HA entities disabled.")
            return
        self._task = asyncio.ensure_future(self._run())

    async def _run(self) -> None:
        while not self._stop:
            try:
                async with aiomqtt.Client(
                    self._cfg["host"], self._cfg.get("port", 1883),
                    username=self._cfg.get("username") or None,
                    password=self._cfg.get("password") or None,
                ) as client:
                    self._client = client
                    await client.publish(AVAILABILITY_TOPIC, "online", retain=True)
                    await self.publish_discovery()
                    await self.publish_state(self._get_state())
                    await client.subscribe(self.clock_state_topic)
                    await client.subscribe(SCREEN_SELECT_COMMAND)
                    await client.subscribe(ROTATION_SWITCH_COMMAND)
                    await client.subscribe(self.clock_button_topic)
                    _LOG.info("MQTT connected - watching %s", self.clock_state_topic)
                    async for message in client.messages:
                        await self._handle(message)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                _LOG.warning("MQTT connection failed (%s) - retrying in 10 s", err)
                self._client = None
                await asyncio.sleep(10)

    async def _handle(self, message) -> None:
        topic = str(message.topic)
        payload = message.payload.decode("utf-8", "replace") if message.payload else ""
        try:
            if topic == self.clock_state_topic:
                state = json.loads(payload)
                if isinstance(state, dict):
                    await self._on_clock_state(state)
            elif topic == SCREEN_SELECT_COMMAND:
                await self._on_select(payload.strip())
            elif topic == self.clock_button_topic:
                if payload.strip() == "next" and self._on_button is not None:
                    await self._on_button()
            elif topic == ROTATION_SWITCH_COMMAND:
                await self._on_rotation(payload.strip().lower() in ("on", "true", "1"))
        except Exception as err:
            _LOG.warning("MQTT message on %s failed: %s", topic, err)

    async def publish_display(self, mode: int) -> None:
        if self._client is not None:
            await self._client.publish(self.clock_display_command_topic, str(mode))
            _LOG.info("Published display=%d to %s", mode, self.clock_display_command_topic)

    async def publish_discovery(self) -> None:
        """(Re)publish HA discovery configs; called again when screens change."""
        if self._client is None:
            return
        state = self._get_state()
        options = state.get("screen_options", [ROTATE, CLOCK])
        select_cfg = {
            "unique_id": f"{BASE_TOPIC}_screen",
            "object_id": f"{BASE_TOPIC}_screen",
            "name": "Tube Display Screen",
            "icon": "mdi:view-dashboard-variant",
            "state_topic": SCREEN_SELECT_STATE,
            "command_topic": SCREEN_SELECT_COMMAND,
            "options": options,
            "availability_topic": AVAILABILITY_TOPIC,
            "device": _DEVICE,
        }
        switch_cfg = {
            "unique_id": f"{BASE_TOPIC}_rotation",
            "object_id": f"{BASE_TOPIC}_rotation",
            "name": "Tube Display Rotation",
            "icon": "mdi:sync",
            "state_topic": ROTATION_SWITCH_STATE,
            "command_topic": ROTATION_SWITCH_COMMAND,
            "payload_on": "on",
            "payload_off": "off",
            "availability_topic": AVAILABILITY_TOPIC,
            "device": _DEVICE,
        }
        await self._client.publish(
            f"homeassistant/select/{BASE_TOPIC}_screen/config",
            json.dumps(select_cfg), retain=True)
        await self._client.publish(
            f"homeassistant/switch/{BASE_TOPIC}_rotation/config",
            json.dumps(switch_cfg), retain=True)
        await self.publish_state(state)
        _LOG.info("Published MQTT discovery (select options: %s)", options)

    async def publish_state(self, state: dict) -> None:
        if self._client is None:
            return
        await self._client.publish(SCREEN_SELECT_STATE, state.get("screen", ROTATE), retain=True)
        await self._client.publish(ROTATION_SWITCH_STATE, state.get("rotation", "on"), retain=True)
        await self._client.publish(STATE_TOPIC, json.dumps(state), retain=True)

    async def stop(self) -> None:
        self._stop = True
        if self._client is not None:
            await self._client.publish(AVAILABILITY_TOPIC, "offline", retain=True)
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
