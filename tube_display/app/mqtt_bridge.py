"""MQTT bridge: one connection, one set of HA entities per clock.

- For every clock subscribes to clock/<id>/volatile/state (display, panel_tubes,
  panels_needed), clock/<id>/button, clock/<id>/availability and
  clock/<id>/ota/state, plus the command topics of that clock's entities, and
  routes each message to the right controller.
- Publishes display mode commands to clock/<id>/set/display and brightness to
  clock/<id>/set/brightness.
- Publishes Home Assistant MQTT discovery per clock. The FIRST clock keeps the
  original entity names (tube_display_screen, tube_display/screen/set, device
  "Tube Display", ...); every other clock <n> uses the prefix tube_display_<n>,
  topics tube_display/<n>/..., names "Tube Display <N> ..." and its own device
  linked via "tube_display".
- Availability is ONE topic for the whole app, tube_display/availability, used by
  every discovery config and set as the connection's last will (retained
  "offline"), so all entities go unavailable when the app dies.

Entities per clock: select <prefix>_screen, switch <prefix>_rotation,
sensor <prefix>_current, sensor <prefix>_last_error,
binary_sensor <prefix>_clock_online, switch <prefix>_pause,
text <prefix>_message, button <prefix>_next.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

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

MAX_ERROR_LEN = 250
MAX_FLASH_SECONDS = 3600
_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

_DEVICE = {
    "identifiers": [BASE_TOPIC],
    "name": "Tube Display",
    "manufacturer": "nict41's HA Apps",
    "model": "Tube Display",
}


class _ClockTopics:
    """All topics and entity names for one clock. The first clock uses the
    original (unprefixed) names so existing HA entities keep working."""

    def __init__(self, name: str, mqtt_id: str, first: bool) -> None:
        self.name = name
        self.mqtt_id = mqtt_id
        self.first = first
        self.prefix = BASE_TOPIC if first else f"{BASE_TOPIC}_{name}"
        self.title = "Tube Display" if first else f"Tube Display {name.title()}"
        self.state_topic = STATE_TOPIC if first else f"{BASE_TOPIC}/{name}/state"
        # ONE availability topic for the whole app (AVAILABILITY_TOPIC): it is also
        # the connection's last will, so every entity goes unavailable if the app dies
        self.availability_topic = AVAILABILITY_TOPIC
        self.select_state = SCREEN_SELECT_STATE if first else f"{BASE_TOPIC}/{name}/screen/state"
        self.select_command = SCREEN_SELECT_COMMAND if first else f"{BASE_TOPIC}/{name}/screen/set"
        self.rotation_state = ROTATION_SWITCH_STATE if first else f"{BASE_TOPIC}/{name}/rotation/state"
        self.rotation_command = ROTATION_SWITCH_COMMAND if first else f"{BASE_TOPIC}/{name}/rotation/set"
        self.current_state = f"{BASE_TOPIC}/current/state" if first else f"{BASE_TOPIC}/{name}/current/state"
        self.last_error_state = f"{BASE_TOPIC}/last_error/state" if first else f"{BASE_TOPIC}/{name}/last_error/state"
        self.online_state = f"{BASE_TOPIC}/clock_online/state" if first else f"{BASE_TOPIC}/{name}/clock_online/state"
        self.pause_state = f"{BASE_TOPIC}/pause/state" if first else f"{BASE_TOPIC}/{name}/pause/state"
        self.pause_command = f"{BASE_TOPIC}/pause/set" if first else f"{BASE_TOPIC}/{name}/pause/set"
        self.message_state = f"{BASE_TOPIC}/message/state" if first else f"{BASE_TOPIC}/{name}/message/state"
        self.message_command = f"{BASE_TOPIC}/message/set" if first else f"{BASE_TOPIC}/{name}/message/set"
        self.next_command = f"{BASE_TOPIC}/next" if first else f"{BASE_TOPIC}/{name}/next"
        self.device = _DEVICE if first else {
            "identifiers": [self.prefix],
            "name": self.title,
            "manufacturer": "nict41's HA Apps",
            "model": "Tube Display",
            "via_device": BASE_TOPIC,
        }

    # clock-side topics (always clock/<id>/...)
    @property
    def clock_state_topic(self) -> str:
        return f"clock/{self.mqtt_id}/volatile/state"

    @property
    def clock_button_topic(self) -> str:
        """The clock's button, pressed while panels show (firmware 2.2.0-nt3+)."""
        return f"clock/{self.mqtt_id}/button"

    @property
    def clock_availability_topic(self) -> str:
        return f"clock/{self.mqtt_id}/availability"

    @property
    def clock_ota_topic(self) -> str:
        return f"clock/{self.mqtt_id}/ota/state"

    @property
    def clock_display_command_topic(self) -> str:
        return f"clock/{self.mqtt_id}/set/display"

    @property
    def clock_brightness_command_topic(self) -> str:
        return f"clock/{self.mqtt_id}/set/brightness"


class MqttBridge:
    """One MQTT connection serving every controller (one per clock)."""

    def __init__(self, mqtt_cfg: dict, controllers: list) -> None:
        """`controllers`: objects with .name and .clock.mqtt_id plus the callbacks
        on_clock_state(dict), on_clock_availability(bool), on_ota_state(dict),
        show_screen(name), clear_pin(), next_screen(), set_rotation_enabled(bool),
        pause(seconds), resume(), flash(...), _mqtt_state() and status()."""
        self._cfg = mqtt_cfg
        self._controllers = list(controllers)
        self._topics = [
            _ClockTopics(c.name, c.clock.mqtt_id, first=(i == 0))
            for i, c in enumerate(self._controllers)
        ]
        self._by_mqtt_id = {t.mqtt_id: t for t in self._topics}
        self._client: aiomqtt.Client | None = None
        self._task: asyncio.Task | None = None
        self._stop = False
        # set after a successful connect, cleared on disconnect: lets other tasks
        # (e.g. the firmware publisher) wait for MQTT before their first publish
        self.connected = asyncio.Event()

    @property
    def enabled(self) -> bool:
        return bool(self._cfg.get("host"))

    @property
    def clock_state_topic(self) -> str:
        """The first clock's state topic (kept for compatibility)."""
        return self._topics[0].clock_state_topic if self._topics else "clock//volatile/state"

    @property
    def clock_button_topic(self) -> str:
        return self._topics[0].clock_button_topic if self._topics else "clock//button"

    @property
    def clock_display_command_topic(self) -> str:
        return self._topics[0].clock_display_command_topic if self._topics else "clock//set/display"

    def _find(self, topic: str) -> tuple[_ClockTopics, object] | None:
        """The (topics, controller) a command/state topic belongs to."""
        for t, c in zip(self._topics, self._controllers):
            if topic in (t.clock_state_topic, t.clock_button_topic, t.clock_availability_topic,
                         t.clock_ota_topic, t.select_command, t.rotation_command,
                         t.pause_command, t.message_command, t.next_command):
                return t, c
        return None

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
                    # last will: if the app dies, every entity goes unavailable
                    will=aiomqtt.Will(AVAILABILITY_TOPIC, "offline", retain=True),
                ) as client:
                    self._client = client
                    self.connected.set()
                    await client.publish(AVAILABILITY_TOPIC, "online", retain=True)
                    await self.publish_discovery()
                    for c in self._controllers:
                        await self.publish_state(c)
                    for t in self._topics:
                        await client.subscribe(t.clock_state_topic)
                        await client.subscribe(t.clock_button_topic)
                        await client.subscribe(t.clock_availability_topic)
                        await client.subscribe(t.clock_ota_topic)
                        await client.subscribe(t.select_command)
                        await client.subscribe(t.rotation_command)
                        await client.subscribe(t.pause_command)
                        await client.subscribe(t.message_command)
                        await client.subscribe(t.next_command)
                    _LOG.info("MQTT connected - watching %d clock(s): %s",
                              len(self._topics), ", ".join(t.clock_state_topic for t in self._topics))
                    async for message in client.messages:
                        await self._handle(message)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                _LOG.warning("MQTT connection failed (%s) - retrying in 10 s", err)
                self._client = None
                self.connected.clear()
                await asyncio.sleep(10)
            else:
                self._client = None
                self.connected.clear()

    async def _handle(self, message) -> None:
        topic = str(message.topic)
        payload = message.payload.decode("utf-8", "replace") if message.payload else ""
        found = self._find(topic)
        if found is None:
            return
        t, ctl = found
        try:
            if topic == t.clock_state_topic:
                state = json.loads(payload)
                if isinstance(state, dict):
                    await ctl.on_clock_state(state)
            elif topic == t.clock_availability_topic:
                await ctl.on_clock_availability(payload.strip().lower() == "online")
            elif topic == t.clock_ota_topic:
                state = json.loads(payload)
                if isinstance(state, dict):
                    await ctl.on_ota_state(state)
                else:
                    _LOG.warning("MQTT: ota state on %s is not a JSON object", topic)
            elif topic == t.select_command:
                await self._on_select(ctl, payload.strip())
            elif topic == t.rotation_command:
                ctl.set_rotation_enabled(payload.strip().lower() in ("on", "true", "1"))
            elif topic == t.clock_button_topic:
                if payload.strip() == "next":
                    ctl.next_screen()
            elif topic == t.pause_command:
                if payload.strip().lower() in ("on", "true", "1"):
                    ctl.pause(None)
                else:
                    ctl.resume()
            elif topic == t.message_command:
                self._on_message(ctl, payload)
            elif topic == t.next_command:
                if payload.strip().upper() == "PRESS":
                    ctl.next_screen()
        except Exception as err:
            _LOG.warning("MQTT message on %s failed: %s", topic, err)

    async def _on_select(self, ctl, value: str) -> None:
        """The screen select: Rotate ends a pin, Clock pins the clock entry,
        anything else pins that screen (unknown names are logged)."""
        if value == ROTATE:
            ctl.clear_pin()
        elif value == CLOCK:
            ctl.show_screen("clock")
        else:
            try:
                ctl.show_screen(value)
            except KeyError:
                _LOG.warning("MQTT select: unknown screen %r", value)

    def _on_message(self, ctl, payload: str) -> None:
        """The message text entity: plain text flashes for 10 s, a JSON object
        {"message", "seconds", "icon", "color"} sets them explicitly. Invalid
        values are logged and ignored (same rules as the HTTP API)."""
        text = payload.strip()
        if not text:
            return
        if text.startswith("{"):
            try:
                data = json.loads(text)
            except ValueError:
                data = None
            if isinstance(data, dict) and isinstance(data.get("message"), str) and data["message"]:
                seconds = data.get("seconds", 10)
                if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) \
                        or seconds <= 0 or seconds > MAX_FLASH_SECONDS:
                    _LOG.warning("MQTT message: 'seconds' must be a number > 0 and <= %d, got %r - ignored",
                                 MAX_FLASH_SECONDS, seconds)
                    return
                icon = data.get("icon")
                if icon is not None and (not isinstance(icon, str) or not icon.startswith("mdi:")):
                    _LOG.warning("MQTT message: 'icon' must be an 'mdi:...' string, got %r - ignored", icon)
                    return
                color = data.get("color")
                if color is not None and (not isinstance(color, str)
                                          or not _COLOR_RE.match(color)):
                    _LOG.warning("MQTT message: 'color' must match #RRGGBB, got %r - ignored", color)
                    return
                ctl.flash(data["message"],
                          seconds=float(seconds),
                          icon=icon,
                          color=color)
                return
            _LOG.warning("MQTT message payload is not a valid message object: %s", text[:120])
            return
        ctl.flash(text)

    # ------------------------------------------------------------------
    # publishing
    # ------------------------------------------------------------------

    def _topic_for_mqtt_id(self, mqtt_id: str) -> _ClockTopics | None:
        return self._by_mqtt_id.get(mqtt_id)

    def _controller_topics(self, ctl) -> _ClockTopics | None:
        for t, c in zip(self._topics, self._controllers):
            if c is ctl:
                return t
        return None

    async def publish_display(self, mqtt_id: str, mode: int) -> None:
        """Publish a display mode command to clock/<id>/set/display."""
        t = self._topic_for_mqtt_id(mqtt_id)
        if self._client is not None and t is not None:
            await self._client.publish(t.clock_display_command_topic, str(mode))
            _LOG.info("Published display=%d to %s", mode, t.clock_display_command_topic)

    async def publish_brightness(self, mqtt_id: str, value: int) -> None:
        """Publish the clock's brightness to clock/<id>/set/brightness."""
        t = self._topic_for_mqtt_id(mqtt_id)
        if self._client is not None and t is not None:
            await self._client.publish(t.clock_brightness_command_topic, str(int(value)))
            _LOG.info("Published brightness=%d to %s", int(value), t.clock_brightness_command_topic)

    async def publish(self, topic: str, payload, retain: bool = True) -> None:
        """Generic publish (used e.g. by the firmware publisher)."""
        if self._client is not None:
            await self._client.publish(topic, payload, retain=retain)

    async def publish_state(self, controller) -> None:
        """Publish every entity's state for one controller (retained) plus the
        JSON status on the clock's state topic."""
        if self._client is None:
            return
        t = self._controller_topics(controller)
        if t is None:
            return
        state = controller._mqtt_state()
        status = controller.status()
        if status.get("ota_busy"):
            shown = "ota"
        elif status.get("message") is not None:
            shown = "message"
        elif status.get("paused"):
            shown = "paused"
        elif status.get("quiet"):
            shown = "quiet"
        elif not status.get("online", True):
            shown = "offline"
        elif status.get("current") in (None, "clock"):
            shown = "clock"
        else:
            shown = status.get("current")
        error = status.get("last_error") or ""
        if len(error) > MAX_ERROR_LEN:
            error = error[:MAX_ERROR_LEN]
        await self._client.publish(t.select_state, state.get("screen", ROTATE), retain=True)
        await self._client.publish(t.rotation_state, state.get("rotation", "on"), retain=True)
        await self._client.publish(t.current_state, str(shown), retain=True)
        await self._client.publish(t.last_error_state, error, retain=True)
        await self._client.publish(t.online_state, "on" if status.get("online", True) else "off", retain=True)
        await self._client.publish(t.pause_state, "on" if status.get("paused") else "off", retain=True)
        await self._client.publish(t.message_state, status.get("message") or "", retain=True)
        await self._client.publish(t.state_topic, json.dumps(status), retain=True)

    async def publish_discovery(self) -> None:
        """(Re)publish HA discovery configs for every clock; called again when screens change."""
        if self._client is None:
            return
        for t, ctl in zip(self._topics, self._controllers):
            state = ctl._mqtt_state()
            options = state.get("screen_options", [ROTATE, CLOCK])
            select_cfg = {
                "unique_id": f"{t.prefix}_screen",
                "object_id": f"{t.prefix}_screen",
                # Just the entity's own name: Home Assistant puts the device name ("Tube Display",
                # "Tube Display Kitchen") in front, so "Tube Display Screen" would show twice
                "name": "Screen",
                "icon": "mdi:view-dashboard-variant",
                "state_topic": t.select_state,
                "command_topic": t.select_command,
                "options": options,
                "availability_topic": t.availability_topic,
                "device": t.device,
            }
            switch_cfg = {
                "unique_id": f"{t.prefix}_rotation",
                "object_id": f"{t.prefix}_rotation",
                "name": "Rotation",
                "icon": "mdi:sync",
                "state_topic": t.rotation_state,
                "command_topic": t.rotation_command,
                "payload_on": "on",
                "payload_off": "off",
                "availability_topic": t.availability_topic,
                "device": t.device,
            }
            current_cfg = {
                "unique_id": f"{t.prefix}_current",
                "object_id": f"{t.prefix}_current",
                "name": "Current Screen",
                "icon": "mdi:television-play",
                "state_topic": t.current_state,
                "availability_topic": t.availability_topic,
                "device": t.device,
            }
            last_error_cfg = {
                "unique_id": f"{t.prefix}_last_error",
                "object_id": f"{t.prefix}_last_error",
                "name": "Last Error",
                "entity_category": "diagnostic",
                "icon": "mdi:alert-circle-outline",
                "state_topic": t.last_error_state,
                "availability_topic": t.availability_topic,
                "device": t.device,
            }
            online_cfg = {
                "unique_id": f"{t.prefix}_clock_online",
                "object_id": f"{t.prefix}_clock_online",
                "name": "Clock Online",
                "device_class": "connectivity",
                "payload_on": "on",
                "payload_off": "off",
                "state_topic": t.online_state,
                "availability_topic": t.availability_topic,
                "device": t.device,
            }
            pause_cfg = {
                "unique_id": f"{t.prefix}_pause",
                "object_id": f"{t.prefix}_pause",
                "name": "Pause",
                "icon": "mdi:pause-circle",
                "state_topic": t.pause_state,
                "command_topic": t.pause_command,
                "payload_on": "on",
                "payload_off": "off",
                "availability_topic": t.availability_topic,
                "device": t.device,
            }
            message_cfg = {
                "unique_id": f"{t.prefix}_message",
                "object_id": f"{t.prefix}_message",
                "name": "Message",
                "icon": "mdi:message-text",
                "max": 255,
                "state_topic": t.message_state,
                "command_topic": t.message_command,
                "availability_topic": t.availability_topic,
                "device": t.device,
            }
            next_cfg = {
                "unique_id": f"{t.prefix}_next",
                "object_id": f"{t.prefix}_next",
                "name": "Next Screen",
                "icon": "mdi:skip-next",
                "command_topic": t.next_command,
                "payload_press": "PRESS",
                "availability_topic": t.availability_topic,
                "device": t.device,
            }
            configs = [
                ("select", f"{t.prefix}_screen", select_cfg),
                ("switch", f"{t.prefix}_rotation", switch_cfg),
                ("sensor", f"{t.prefix}_current", current_cfg),
                ("sensor", f"{t.prefix}_last_error", last_error_cfg),
                ("binary_sensor", f"{t.prefix}_clock_online", online_cfg),
                ("switch", f"{t.prefix}_pause", pause_cfg),
                ("text", f"{t.prefix}_message", message_cfg),
                ("button", f"{t.prefix}_next", next_cfg),
            ]
            for component, uid, cfg in configs:
                await self._client.publish(
                    f"homeassistant/{component}/{uid}/config",
                    json.dumps(cfg), retain=True)
            await self.publish_state(ctl)
        _LOG.info("Published MQTT discovery for %d clock(s)", len(self._topics))

    async def stop(self) -> None:
        self._stop = True
        if self._client is not None:
            await self._client.publish(AVAILABILITY_TOPIC, "offline", retain=True)
        self.connected.clear()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
