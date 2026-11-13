"""Home Assistant access through the Supervisor's API proxy.

Templates are rendered with POST /api/template, history with
/api/history/period/..., camera snapshots with /api/camera_proxy/<entity>.
All go to http://supervisor/core/api with the add-on's SUPERVISOR_TOKEN.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp

_LOG = logging.getLogger(__name__)

SUPERVISOR_API = "http://supervisor/core/api"


class HaClient:
    """Thin async wrapper around the Supervisor-proxied HA REST API."""

    def __init__(self, session: aiohttp.ClientSession, token: str | None = None,
                 base_url: str = SUPERVISOR_API) -> None:
        self._session = session
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._base = base_url.rstrip("/")

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        kwargs.setdefault("timeout", aiohttp.ClientTimeout(total=30))
        async with self._session.request(method, f"{self._base}{path}",
                                         headers=self._headers, **kwargs) as resp:
            if resp.status >= 400:
                body = await resp.text()
                raise HaError(f"HA API {method} {path} -> {resp.status}: {body[:200]}")
            if resp.content_type == "application/json":
                return await resp.json()
            return await resp.read()

    async def render_template(self, template: str) -> str:
        """Render one HA template. Returns the rendered string."""
        result = await self._request("POST", "/template", json={"template": template})
        # HA answers text/plain, which _request returns as bytes; str(bytes) would give "b'42'"
        if isinstance(result, (bytes, bytearray)):
            return result.decode("utf-8", errors="replace")
        return str(result)

    async def render_templates(self, templates: list[str]) -> list[str]:
        """Render several templates concurrently, preserving order."""
        if not templates:
            return []
        results = await asyncio.gather(
            *(self.render_template(t) for t in templates), return_exceptions=True
        )
        out = []
        for t, r in zip(templates, results):
            if isinstance(r, Exception):
                raise r
            out.append(r)
        return out

    async def history(self, entity_id: str, hours: float) -> list[dict]:
        """State history for an entity over the last `hours` hours."""
        start = datetime.now(timezone.utc) - timedelta(hours=hours)
        params = {
            "filter_entity_id": entity_id,
            "minimal_response": "true",
            "no_attributes": "true",
            "end_time": datetime.now(timezone.utc).isoformat(),
        }
        result = await self._request(
            "GET", f"/history/period/{start.isoformat()}", params=params
        )
        # HA returns [[state, ...], ...] - one list per entity.
        if isinstance(result, list) and result and isinstance(result[0], list):
            return result[0]
        return []

    async def state(self, entity_id: str) -> dict | None:
        """The full state object of an entity (state + attributes), or None if it doesn't exist."""
        try:
            result = await self._request("GET", f"/states/{entity_id}")
        except HaError:
            return None
        return result if isinstance(result, dict) else None

    async def forecast_daily(self, entity_id: str) -> list[dict]:
        """The daily forecast of a weather entity via the weather.get_forecasts service.

        Returns the list of forecast dicts (datetime, temperature, templow, ...), newest
        first as HA sends them; empty when the entity has none or the call fails."""
        try:
            result = await self._request(
                "POST", "/services/weather/get_forecasts",
                params={"return_response": "true"},
                json={"entity_id": entity_id, "type": "daily"},
            )
        except HaError:
            return []
        # HA wraps the answer: {"service_response": {"<entity>": {"forecast": [...]}}};
        # accept the bare {"<entity>": {...}} / {"forecast": [...]} shapes too
        if isinstance(result, dict):
            resp = result.get("service_response", result)
            if isinstance(resp, dict):
                entry = resp.get(entity_id)
                if isinstance(entry, dict) and isinstance(entry.get("forecast"), list):
                    return entry["forecast"]
                if isinstance(resp.get("forecast"), list):
                    return resp["forecast"]
        return []

    async def camera_image(self, entity_id: str) -> bytes:
        """Current snapshot of a camera entity via /api/camera_proxy."""
        return await self._request("GET", f"/camera_proxy/{entity_id}")

    async def entity_picture(self, entity_id: str) -> bytes | None:
        """The entity_picture of any entity, or None if it has none."""
        try:
            states = await self._request("GET", f"/states/{entity_id}")
        except HaError:
            return None
        url = (states or {}).get("attributes", {}).get("entity_picture")
        if not url:
            return None
        if url.startswith("/"):
            url = f"{self._base}{url}"
        async with self._session.get(url, headers=self._headers,
                                     timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status >= 400:
                return None
            return await resp.read()


class HaError(Exception):
    pass
