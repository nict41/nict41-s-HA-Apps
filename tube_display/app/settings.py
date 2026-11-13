"""Add-on options, read from the environment the run script exports."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field

LOG_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

DEFAULT_SCREENS_FILE = "/config/screens.yaml"
API_PORT = 8098

CLOCK_NAME_RE = re.compile(r"^[a-z0-9_]{1,20}$")


def _base_url(host: str) -> str:
    """A clock host as a base URL: http:// added unless it already has a scheme."""
    host = host.strip()
    if host.startswith("http://") or host.startswith("https://"):
        return host.rstrip("/")
    return f"http://{host}"


@dataclass
class ClockSettings:
    """One physical clock: where to push pictures and how to reach it over MQTT."""

    name: str
    host: str
    mqtt_id: str
    user: str = ""
    password: str = ""

    @property
    def base_url(self) -> str:
        return _base_url(self.host)


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
    clocks: list[ClockSettings] = field(default_factory=list)
    app_url: str = ""            # public URL of this app (env TUBE_APP_URL), e.g. for firmware links
    firmware_updates: bool = True   # env TUBE_FIRMWARE_UPDATES ("true"/"false")
    firmware_variant: str = "ipstube_ha"  # env TUBE_FIRMWARE_VARIANT
    errors: list[str] = field(default_factory=list)  # config problems that did not stop startup

    def __post_init__(self) -> None:
        # Built directly (tests, embedding) with only the legacy single-clock fields:
        # behave exactly like the legacy config - one clock named "main".
        if not self.clocks and self.clock_host.strip():
            self.clocks = [ClockSettings(
                name="main", host=self.clock_host, mqtt_id=self.clock_mqtt_id,
                user=self.clock_user, password=self.clock_password,
            )]

    @property
    def clock_base_url(self) -> str:
        return _base_url(self.clock_host)

    @property
    def primary_clock(self) -> ClockSettings | None:
        """The first configured clock (the legacy one when only clock_host is set)."""
        return self.clocks[0] if self.clocks else None

    @property
    def logging_level(self) -> int:
        return LOG_LEVELS.get(self.log_level, logging.INFO)


def _parse_clocks_json(raw: str, errors: list[str]) -> list[ClockSettings]:
    """TUBE_CLOCKS_JSON: a JSON list of {name, host, mqtt_id, user?, password?}.

    Bad entries are skipped with a message in `errors`; empty/invalid JSON yields
    an error message and no clocks (the legacy clock_host still applies)."""
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError as err:
        errors.append(f"TUBE_CLOCKS_JSON is not valid JSON ({err}) - ignored")
        return []
    if not isinstance(data, list):
        errors.append("TUBE_CLOCKS_JSON must be a JSON list of clock objects - ignored")
        return []
    clocks: list[ClockSettings] = []
    seen: set[str] = set()
    for i, entry in enumerate(data):
        where = f"TUBE_CLOCKS_JSON entry {i}"
        if not isinstance(entry, dict):
            errors.append(f"{where}: must be an object, got {type(entry).__name__} - skipped")
            continue
        name = str(entry.get("name", "")).strip().lower()
        host = str(entry.get("host", "")).strip()
        mqtt_id = str(entry.get("mqtt_id", "")).strip()
        if not CLOCK_NAME_RE.match(name):
            errors.append(f"{where}: 'name' must match ^[a-z0-9_]{{1,20}}$ (lower-cased), got {name!r} - skipped")
            continue
        if name in seen:
            errors.append(f"{where}: duplicate clock name {name!r} - skipped")
            continue
        if not host:
            errors.append(f"{where} ({name!r}): 'host' is required - skipped")
            continue
        if not mqtt_id:
            errors.append(f"{where} ({name!r}): 'mqtt_id' is required - skipped")
            continue
        seen.add(name)
        clocks.append(ClockSettings(
            name=name, host=host, mqtt_id=mqtt_id,
            user=str(entry.get("user", "") or ""),
            password=str(entry.get("password", "") or ""),
        ))
    return clocks


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
    errors: list[str] = []
    clocks: list[ClockSettings] = []
    # The legacy single clock comes first, so it keeps the original MQTT entity names
    legacy_host = env.get("TUBE_CLOCK_HOST", "").strip()
    if legacy_host:
        clocks.append(ClockSettings(
            name="main", host=legacy_host,
            mqtt_id=env.get("TUBE_CLOCK_MQTT_ID", "").strip(),
            user=env.get("TUBE_CLOCK_USER", ""),
            password=env.get("TUBE_CLOCK_PASSWORD", ""),
        ))
    clocks += _parse_clocks_json(env.get("TUBE_CLOCKS_JSON", ""), errors)
    # a duplicate "main" from TUBE_CLOCKS_JSON would clash with the legacy entity names
    names = [c.name for c in clocks]
    for name in set(names):
        if names.count(name) > 1:
            errors.append(f"clock name {name!r} is used more than once - only the first is kept")
            while names.count(name) > 1:
                idx = names.index(name, names.index(name) + 1)
                clocks.pop(idx)
                names.pop(idx)
    fw = env.get("TUBE_FIRMWARE_UPDATES", "true").strip().lower()
    return Settings(
        clock_host=legacy_host,
        clock_mqtt_id=env.get("TUBE_CLOCK_MQTT_ID", "").strip(),
        clock_user=env.get("TUBE_CLOCK_USER", ""),
        clock_password=env.get("TUBE_CLOCK_PASSWORD", ""),
        default_duration=_int("TUBE_DEFAULT_DURATION", 15),
        default_refresh=_int("TUBE_DEFAULT_REFRESH", 30),
        log_level=env.get("TUBE_LOG_LEVEL", "info").strip().lower(),
        screens_file=env.get("TUBE_SCREENS_FILE", DEFAULT_SCREENS_FILE),
        mqtt=mqtt,
        clocks=clocks,
        app_url=env.get("TUBE_APP_URL", "").strip(),
        firmware_updates=fw not in ("false", "0", "no", "off"),
        firmware_variant=env.get("TUBE_FIRMWARE_VARIANT", "ipstube_ha").strip() or "ipstube_ha",
        errors=errors,
    )
