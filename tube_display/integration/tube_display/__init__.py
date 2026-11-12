"""Tube Display: Home Assistant actions for the Tube Display app.

The app (nict41's HA Apps) renders Home Assistant information onto an IPSTube
clock running HA Tubes. Its entities (screen select, rotation switch, ...) come
over MQTT discovery; this integration only adds named actions, which call the
app's HTTP API:

    tube_display.show_screen    POST /api/show
    tube_display.flash_message  POST /api/flash
    tube_display.pause          POST /api/pause
    tube_display.resume         POST /api/resume
    tube_display.next_screen    POST /api/next
    tube_display.set_rotation   POST /api/rotation

The app installs and updates this integration itself and announces its address
through Supervisor discovery, so setting it up is one click.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_CLOCK,
    DOMAIN,
    SERVICE_FLASH_MESSAGE,
    SERVICE_NEXT_SCREEN,
    SERVICE_PAUSE,
    SERVICE_RESUME,
    SERVICE_SET_ROTATION,
    SERVICE_SHOW_SCREEN,
)

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

_CLOCK = {vol.Optional(CONF_CLOCK): cv.string}
_SECONDS = vol.All(vol.Coerce(float), vol.Range(min=1, max=86400))

SCHEMAS: dict[str, vol.Schema] = {
    SERVICE_SHOW_SCREEN: vol.Schema({
        vol.Required("screen"): cv.string,
        vol.Optional("seconds"): _SECONDS,
        **_CLOCK,
    }),
    SERVICE_FLASH_MESSAGE: vol.Schema({
        vol.Required("message"): cv.string,
        vol.Optional("seconds", default=10): _SECONDS,
        vol.Optional("icon"): cv.icon,
        vol.Optional("color"): vol.Match(r"^#?[0-9A-Fa-f]{6}$"),
        **_CLOCK,
    }),
    SERVICE_PAUSE: vol.Schema({vol.Optional("seconds"): _SECONDS, **_CLOCK}),
    SERVICE_RESUME: vol.Schema(_CLOCK),
    SERVICE_NEXT_SCREEN: vol.Schema(_CLOCK),
    SERVICE_SET_ROTATION: vol.Schema({vol.Required("enabled"): cv.boolean, **_CLOCK}),
}

PATHS = {
    SERVICE_SHOW_SCREEN: "/api/show",
    SERVICE_FLASH_MESSAGE: "/api/flash",
    SERVICE_PAUSE: "/api/pause",
    SERVICE_RESUME: "/api/resume",
    SERVICE_NEXT_SCREEN: "/api/next",
    SERVICE_SET_ROTATION: "/api/rotation",
}


def _base_url(entry: ConfigEntry) -> str:
    return f"http://{entry.data[CONF_HOST]}:{entry.data[CONF_PORT]}"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})["base_url"] = _base_url(entry)
    session = async_get_clientsession(hass)

    async def call_app(call: ServiceCall) -> dict[str, Any]:
        base = hass.data[DOMAIN].get("base_url")
        if not base:
            raise HomeAssistantError("Tube Display is not set up")
        body = dict(call.data)
        if body.get("color") and not str(body["color"]).startswith("#"):
            body["color"] = "#" + body["color"]
        url = base + PATHS[call.service]
        try:
            async with session.post(url, json=body, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                try:
                    answer = await resp.json(content_type=None)
                except ValueError:
                    answer = {}
                if resp.status >= 400:
                    msg = answer.get("error") if isinstance(answer, dict) else None
                    if 400 <= resp.status < 500:
                        raise ServiceValidationError(msg or f"Tube Display refused: HTTP {resp.status}")
                    raise HomeAssistantError(msg or f"Tube Display: HTTP {resp.status}")
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise HomeAssistantError(f"Tube Display app not reachable at {base}: {err}") from err
        return answer if isinstance(answer, dict) else {}

    for service, schema in SCHEMAS.items():
        hass.services.async_register(DOMAIN, service, call_app, schema=schema,
                                     supports_response=SupportsResponse.OPTIONAL)
    entry.async_on_unload(entry.add_update_listener(_updated))
    return True


async def _updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    hass.data.setdefault(DOMAIN, {})["base_url"] = _base_url(entry)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    for service in SCHEMAS:
        hass.services.async_remove(DOMAIN, service)
    hass.data.pop(DOMAIN, None)
    return True
