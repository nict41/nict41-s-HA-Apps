"""Home Assistant MQTT discovery sensors.

Entirely optional: when no broker is configured every call here is a no-op and
the add-on still mirrors playlists. One retained JSON state topic feeds all the
entities through value templates, so a cycle publishes once rather than six
times.
"""

from __future__ import annotations

import json
import logging

from settings import Config

_LOG = logging.getLogger(__name__)

BASE_TOPIC = "youtube_music_sync"
STATE_TOPIC = f"{BASE_TOPIC}/state"
ATTR_TOPIC = f"{BASE_TOPIC}/attributes"
AVAILABILITY_TOPIC = f"{BASE_TOPIC}/availability"

_DEVICE = {
    "identifiers": [BASE_TOPIC],
    "name": "YouTube Music Sync",
    "manufacturer": "nict41's HA Apps",
    "model": "YouTube Music Sync",
}

# (object_id, component, config). unique_id is pinned so restarts and
# reinstalls keep driving the same entities rather than making duplicates.
_ENTITIES = (
    ("tracks_downloaded", "sensor", {
        "name": "Tracks downloaded",
        "icon": "mdi:music-note-plus",
        "unit_of_measurement": "tracks",
        "state_class": "total_increasing",
        "value_template": "{{ value_json.downloaded }}",
        "json_attributes_topic": ATTR_TOPIC,
    }),
    ("duplicates_skipped", "sensor", {
        "name": "Duplicates skipped",
        "icon": "mdi:content-duplicate",
        "unit_of_measurement": "tracks",
        "state_class": "total_increasing",
        "value_template": "{{ value_json.skipped }}",
    }),
    ("library_tracks", "sensor", {
        "name": "Library tracks",
        "icon": "mdi:library-music",
        "unit_of_measurement": "tracks",
        "value_template": "{{ value_json.library_files }}",
    }),
    ("last_run", "sensor", {
        "name": "Last run",
        "icon": "mdi:clock-check-outline",
        "device_class": "timestamp",
        "value_template": "{{ value_json.last_run }}",
    }),
    ("status", "sensor", {
        "name": "Status",
        "icon": "mdi:playlist-music",
        "value_template": "{{ value_json.status }}",
    }),
    ("problem", "binary_sensor", {
        "name": "Problem",
        "device_class": "problem",
        "value_template": "{{ value_json.problem }}",
        "payload_on": "on",
        "payload_off": "off",
        "json_attributes_topic": ATTR_TOPIC,
    }),
)


class Publisher:
    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._client = None

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def connect(self) -> bool:
        if not self._cfg.mqtt:
            return False
        try:
            import paho.mqtt.client as mqtt

            client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2, client_id=f"{BASE_TOPIC}-addon"
            )
            if self._cfg.mqtt.username:
                client.username_pw_set(
                    self._cfg.mqtt.username, self._cfg.mqtt.password or None
                )
            # Last will, so the entities go unavailable if this add-on dies
            # rather than showing a stale "all good" reading forever.
            client.will_set(AVAILABILITY_TOPIC, "offline", retain=True)
            client.connect(self._cfg.mqtt.host, self._cfg.mqtt.port, keepalive=60)
            client.loop_start()
            self._client = client
            client.publish(AVAILABILITY_TOPIC, "online", retain=True)
            _LOG.info("Connected to MQTT broker at %s:%s", self._cfg.mqtt.host, self._cfg.mqtt.port)
            return True
        except Exception as err:
            _LOG.warning("Could not connect to MQTT (%s) - continuing without sensors.", err)
            self._client = None
            return False

    def publish_discovery(self) -> None:
        if not self._client:
            return
        for object_id, component, extra in _ENTITIES:
            config = {
                "unique_id": f"{BASE_TOPIC}_{object_id}",
                "object_id": f"{BASE_TOPIC}_{object_id}",
                "state_topic": STATE_TOPIC,
                "availability_topic": AVAILABILITY_TOPIC,
                "device": _DEVICE,
                **extra,
            }
            topic = f"homeassistant/{component}/{BASE_TOPIC}_{object_id}/config"
            self._client.publish(topic, json.dumps(config), retain=True)
        _LOG.info("Published MQTT discovery for %d entities.", len(_ENTITIES))

    def publish_state(self, state: dict, attributes: dict | None = None) -> None:
        if not self._client:
            return
        try:
            self._client.publish(STATE_TOPIC, json.dumps(state), retain=True)
            if attributes is not None:
                self._client.publish(ATTR_TOPIC, json.dumps(attributes), retain=True)
        except Exception as err:
            _LOG.warning("MQTT publish failed: %s", err)

    def close(self) -> None:
        if not self._client:
            return
        try:
            self._client.publish(AVAILABILITY_TOPIC, "offline", retain=True)
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:
            pass
        self._client = None
