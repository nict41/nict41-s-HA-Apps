"""Installs the bundled `tube_display` integration and announces the app to HA.

The integration (custom_components/tube_display) adds named actions such as
tube_display.flash_message. It ships inside the app image at /app/integration
and is copied into Home Assistant's config folder when it's missing or the
bundled version differs; Home Assistant then needs one restart to load it, so
a persistent notification says so (the app never restarts Home Assistant).

The app also posts a Supervisor discovery message (service "tube_display"
with its host name and API port), which Home Assistant shows as a discovered
integration - set-up is one click.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket

import aiohttp

_LOG = logging.getLogger(__name__)

DOMAIN = "tube_display"
BUNDLED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "integration", DOMAIN)
TARGET_ROOT = "/homeassistant/custom_components"


def _version(folder: str) -> str | None:
    try:
        with open(os.path.join(folder, "manifest.json"), encoding="utf-8") as f:
            return str(json.load(f).get("version"))
    except (OSError, ValueError):
        return None


def install(bundled: str = BUNDLED, target_root: str = TARGET_ROOT) -> str | None:
    """Copy the integration if missing or a different version. Returns the version
    installed now, or None if nothing changed (or it failed)."""
    want = _version(bundled)
    if want is None:
        _LOG.warning("Bundled integration not found at %s", bundled)
        return None
    target = os.path.join(target_root, DOMAIN)
    have = _version(target)
    if have == want:
        return None
    if not os.path.isdir(target_root):
        _LOG.warning("%s not found - not installing the Tube Display integration", target_root)
        return None
    tmp = target + ".new"
    try:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.copytree(bundled, tmp, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.rmtree(target, ignore_errors=True)
        os.replace(tmp, target)
    except OSError as err:
        _LOG.error("Could not install the Tube Display integration: %s", err)
        shutil.rmtree(tmp, ignore_errors=True)
        return None
    _LOG.info("Installed the Tube Display integration %s (was %s) - restart Home Assistant to load it",
              want, have or "not installed")
    return want


async def notify_restart(session: aiohttp.ClientSession, token: str, version: str, was_installed: bool,
                         core_api: str = "http://supervisor/core/api") -> None:
    verb = "updated" if was_installed else "installed"
    body = {
        "notification_id": "tube_display_integration",
        "title": "Tube Display",
        "message": (f"The Tube Display integration was {verb} (version {version}). Restart Home Assistant "
                    "to load it; then add it under Settings > Devices & services, where it shows as "
                    "discovered, to get actions like tube_display.flash_message."),
    }
    try:
        async with session.post(f"{core_api}/services/persistent_notification/create", json=body,
                                headers={"Authorization": f"Bearer {token}"},
                                timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status >= 400:
                _LOG.warning("Could not create the restart notification: HTTP %d", resp.status)
    except (aiohttp.ClientError, TimeoutError) as err:
        _LOG.warning("Could not create the restart notification: %s", err)


async def announce(session: aiohttp.ClientSession, token: str, port: int,
                   supervisor: str = "http://supervisor", host: str | None = None) -> bool:
    """Supervisor discovery: lets Home Assistant offer the integration with this address."""
    body = {"service": DOMAIN, "config": {"host": host or socket.gethostname(), "port": port}}
    try:
        async with session.post(f"{supervisor}/discovery", json=body,
                                headers={"Authorization": f"Bearer {token}"},
                                timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status >= 400:
                _LOG.warning("Supervisor discovery failed: HTTP %d %s", resp.status, (await resp.text())[:200])
                return False
            return True
    except (aiohttp.ClientError, TimeoutError) as err:
        _LOG.warning("Supervisor discovery failed: %s", err)
        return False
