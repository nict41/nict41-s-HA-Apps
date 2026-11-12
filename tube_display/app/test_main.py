"""Smoke test for main.run(): with no SUPERVISOR_TOKEN and no MQTT configured the
app must still start the HTTP API (and the editor) and answer /api/status - even
with no clocks configured - and shut down cleanly when the task is cancelled.

Runs main.run() on free ports with a fake environment; no network beyond the
local test server.
"""

import asyncio
import socket

import pytest

from conftest import pil, requires_pil

pytestmark = requires_pil

if pil:
    import aiohttp
    import editor as editor_mod
    import main as main_mod
    import settings as settings_mod


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_main_run_serves_api_status_without_token_or_mqtt(tmp_path, monkeypatch):
    api_port = _free_port()
    ingress_port = _free_port()
    screens_file = tmp_path / "screens.yaml"
    screens_file.write_text(
        "rotation: [a]\nscreens:\n  a:\n    tubes:\n"
        + "      - {type: blank}\n" * 6, encoding="utf-8")

    env = {
        "TUBE_SCREENS_FILE": str(screens_file),
        # no TUBE_CLOCK_HOST, no TUBE_CLOCKS_JSON, no TUBE_MQTT_HOST, no SUPERVISOR_TOKEN
    }
    monkeypatch.setattr(settings_mod, "API_PORT", api_port)
    monkeypatch.setattr(main_mod, "API_PORT", api_port)
    monkeypatch.setattr(editor_mod, "INGRESS_PORT", ingress_port)
    monkeypatch.setattr(main_mod, "load_settings", lambda env=None: settings_mod.load_settings(env))
    for key in ("SUPERVISOR_TOKEN", "TUBE_CLOCK_HOST", "TUBE_CLOCKS_JSON",
                "TUBE_MQTT_HOST", "TUBE_APP_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(main_mod.os, "environ", {**env})

    async def drive():
        task = asyncio.ensure_future(main_mod.run())
        try:
            # wait until the API answers (or give up after ~5 s of real time)
            async with aiohttp.ClientSession() as session:
                status = None
                for _ in range(100):
                    await asyncio.sleep(0.05)
                    try:
                        async with session.get(f"http://127.0.0.1:{api_port}/api/status",
                                               timeout=aiohttp.ClientTimeout(total=1)) as resp:
                            status = (resp.status, await resp.json())
                            break
                    except Exception:
                        continue
                assert status is not None, "HTTP API never came up"
                code, data = status
                assert code == 200
                # no clocks configured: empty clocks, but the API answers
                assert data["clocks"] == {}
                assert data["templates"] is None      # no SUPERVISOR_TOKEN -> no cache
                assert data["firmware"] is None       # no MQTT -> no publisher
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    asyncio.run(drive())
