"""Add-on options, read from the environment the run script exports."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

LOG_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

DEFAULT_SCREENS_FILE = "/config/screens.yaml"
API_PORT = 8098


@dataclass
class Settings:
    clock_host: str
    clock_mqtt_id: str
    clock_user: str = ""
    clock_password: str = ""
    default_duration: int = 15
    default_refresh: int = 30
    log_level: str = "info"
    screens_file: str = DEFAULT_SCREENS_FILE
    mqtt: dict = field(default_factory=dict)  # host/port/username/password, may be empty

    @property
    def clock_base_url(self) -> str:
        host = self.clock_host.strip()
        if host.startswith("http://") or host.startswith("https://"):
            return host.rstrip("/")
        return f"http://{host}"

    @property
    def logging_level(self) -> int:
        return LOG_LEVELS.get(self.log_level, logging.INFO)


def load_settings(env: dict | None = None) -> Settings:
    env = os.environ if env is None else env

    def _int(name: str, default: int) -> int:
        try:
            return int(env.get(name, str(default)))
        except (TypeError, ValueError):
            return default

    mqtt = {}
    if env.get("TUBE_MQTT_HOST"):
        mqtt = {
            "host": env["TUBE_MQTT_HOST"],
            "port": _int("TUBE_MQTT_PORT", 1883),
            "username": env.get("TUBE_MQTT_USER", ""),
            "password": env.get("TUBE_MQTT_PASS", ""),
        }
    return Settings(
        clock_host=env.get("TUBE_CLOCK_HOST", "").strip(),
        clock_mqtt_id=env.get("TUBE_CLOCK_MQTT_ID", "").strip(),
        clock_user=env.get("TUBE_CLOCK_USER", ""),
        clock_password=env.get("TUBE_CLOCK_PASSWORD", ""),
        default_duration=_int("TUBE_DEFAULT_DURATION", 15),
        default_refresh=_int("TUBE_DEFAULT_REFRESH", 30),
        log_level=env.get("TUBE_LOG_LEVEL", "info").strip().lower(),
        screens_file=env.get("TUBE_SCREENS_FILE", DEFAULT_SCREENS_FILE),
        mqtt=mqtt,
    )
