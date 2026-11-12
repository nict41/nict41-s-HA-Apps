"""Config flow: found through Supervisor discovery, or entered by hand."""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.helpers.aiohttp_client import async_get_clientsession

try:
    from homeassistant.helpers.service_info.hassio import HassioServiceInfo
except ImportError:  # older Home Assistant
    from homeassistant.components.hassio import HassioServiceInfo  # type: ignore[no-redef]

from .const import DEFAULT_PORT, DOMAIN


async def _reachable(hass, host: str, port: int) -> bool:
    session = async_get_clientsession(hass)
    try:
        async with session.get(f"http://{host}:{port}/api/status",
                               timeout=aiohttp.ClientTimeout(total=10)) as resp:
            return resp.status == 200
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return False


class TubeDisplayConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._discovered: dict[str, Any] = {}

    async def async_step_hassio(self, discovery_info: HassioServiceInfo) -> ConfigFlowResult:
        """The Tube Display app announced itself through the Supervisor."""
        host = discovery_info.config.get(CONF_HOST)
        port = int(discovery_info.config.get(CONF_PORT, DEFAULT_PORT))
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured(updates={CONF_HOST: host, CONF_PORT: port})
        self._discovered = {CONF_HOST: host, CONF_PORT: port}
        return await self.async_step_hassio_confirm()

    async def async_step_hassio_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="Tube Display", data=self._discovered)
        return self.async_show_form(step_id="hassio_confirm")

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = int(user_input[CONF_PORT])
            if await _reachable(self.hass, host, port):
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured(updates={CONF_HOST: host, CONF_PORT: port})
                return self.async_create_entry(title="Tube Display", data={CONF_HOST: host, CONF_PORT: port})
            errors["base"] = "cannot_connect"
        schema = vol.Schema({
            vol.Required(CONF_HOST, default=(user_input or {}).get(CONF_HOST, "")): str,
            vol.Required(CONF_PORT, default=(user_input or {}).get(CONF_PORT, DEFAULT_PORT)): int,
        })
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)
