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

It also polls the app's GET /api/status every 10 minutes (and once at setup) and
turns the clock notices it reports (features the screens use that the clock's
firmware cannot do) into Repairs issues.

The app installs and updates this integration itself and announces its address
through Supervisor discovery, so setting it up is one click.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.issue_registry import (
    IssueSeverity,
    async_create_issue,
    async_delete_issue,
    async_get as async_get_issue_registry,
)
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    CONF_CLOCK,
    DOMAIN,
    NOTICE_CODES,
    SERVICE_FLASH_MESSAGE,
    SERVICE_NEXT_SCREEN,
    SERVICE_PAUSE,
    SERVICE_RESUME,
    SERVICE_SET_ROTATION,
    SERVICE_SHOW_SCREEN,
)

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

# GET /api/status is polled once at setup and then every 10 minutes
ISSUES_INTERVAL = 600.0

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

    # Repairs issues for the clock notices the app reports (firmware too old for a
    # feature a screen uses): polled once now and then every ISSUES_INTERVAL s
    async def poll_issues(_now=None) -> None:     # the interval passes the time
        await _sync_issues(hass, session, hass.data[DOMAIN]["base_url"])

    cancel = async_track_time_interval(hass, poll_issues, timedelta(seconds=ISSUES_INTERVAL))
    entry.async_on_unload(cancel)
    # the first poll in the background, so a slow app doesn't hold up the setup
    entry.async_create_background_task(hass, poll_issues(), "tube_display_issues")
    return True


async def _sync_issues(hass: HomeAssistant, session: aiohttp.ClientSession, base: str) -> None:
    """GET the app's /api/status and make one Repairs issue per notice of each clock
    (issue_id "<code>_<clock>"); issues whose notice disappeared are deleted. Network
    errors are logged at debug and leave the existing issues alone."""
    try:
        async with session.get(base + "/api/status", timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                _LOGGER.debug("Tube Display /api/status answered HTTP %s - keeping the issues", resp.status)
                return
            status = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
        _LOGGER.debug("Tube Display /api/status not reachable at %s: %s - keeping the issues", base, err)
        return
    clocks = status.get("clocks") if isinstance(status, dict) else None
    if not isinstance(clocks, dict):
        return
    wanted: set[str] = set()
    for clock_name, clock_status in clocks.items():
        if not isinstance(clock_status, dict):
            continue
        info = clock_status.get("clock_info")
        if not isinstance(info, dict):
            continue
        notices = info.get("notices")
        if not isinstance(notices, list):
            continue
        for notice in notices:
            if not isinstance(notice, dict):
                continue
            code = notice.get("code")
            if code not in NOTICE_CODES:
                continue
            issue_id = f"{code}_{clock_name}"
            wanted.add(issue_id)
            async_create_issue(
                hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=IssueSeverity.WARNING,
                translation_key=str(code),
                translation_placeholders={"clock": str(clock_name)},
            )
    # delete the issues whose notice is gone (only ours, only known codes)
    registry = async_get_issue_registry(hass)
    for issue in list(registry.issues.values()):
        if issue.domain != DOMAIN or issue.is_fixable is not False:
            continue
        # our issues are "<code>_<clock>"; the clock name may itself contain "_"
        code = next((c for c in NOTICE_CODES if issue.issue_id.startswith(c + "_")), None)
        if code is not None and issue.issue_id not in wanted:
            async_delete_issue(hass, DOMAIN, issue.issue_id)


async def _updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    hass.data.setdefault(DOMAIN, {})["base_url"] = _base_url(entry)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    for service in SCHEMAS:
        hass.services.async_remove(DOMAIN, service)
    hass.data.pop(DOMAIN, None)
    return True
