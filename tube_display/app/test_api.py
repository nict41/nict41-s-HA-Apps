"""HTTP API tests: /api/status, /api/show (interrupt + resume), /api/rotation,
/preview/<screen>.png. Uses aiohttp's test utils with a stub controller - no
network beyond the local test server.
"""

import asyncio
import io

import pytest
from aiohttp.test_utils import TestClient, TestServer

from conftest import pil, requires_pil

pytestmark = requires_pil

if pil:
    from PIL import Image
    import api as api_mod
    from rgb565 import STRIP_W, STRIP_H


class StubController:
    def __init__(self):
        self.current = "a"
        self.rotation_enabled = True
        self.shown = []
        self.rotation_calls = []

    def status(self):
        return {
            "current": self.current,
            "rotation": ["a", "clock"],
            "rotation_enabled": self.rotation_enabled,
            "pinned": None,
            "screens": ["a"],
            "config_errors": [],
            "last_error": None,
            "clock_state": {"display": 4},
            "clock_host": "clock.test",
        }

    def show_screen(self, name, seconds=None):
        if name not in ("a", "clock"):
            raise KeyError(name)
        self.shown.append((name, seconds))
        self.current = name

    def set_rotation_enabled(self, enabled):
        self.rotation_enabled = enabled
        self.rotation_calls.append(enabled)

    async def render_preview(self, name):
        if name != "a":
            raise KeyError(name)
        return Image.new("RGB", (STRIP_W, STRIP_H), (20, 40, 60))


def _run(coro):
    return asyncio.run(coro)


def _with_client(fn):
    async def wrapper():
        controller = StubController()
        app = api_mod.create_app(controller)
        async with TestClient(TestServer(app)) as client:
            await fn(client, controller)
    return asyncio.run(wrapper())


def test_api_status():
    async def check(client, controller):
        resp = await client.get("/api/status")
        assert resp.status == 200
        data = await resp.json()
        assert data["current"] == "a"
        assert data["rotation"] == ["a", "clock"]
        assert data["clock_state"] == {"display": 4}
    _with_client(check)


def test_api_show_interrupt():
    async def check(client, controller):
        resp = await client.post("/api/show", json={"screen": "a", "seconds": 30})
        assert resp.status == 200
        data = await resp.json()
        assert data == {"ok": True, "screen": "a", "seconds": 30.0}
        assert controller.shown == [("a", 30.0)]
    _with_client(check)


def test_api_show_clock_entry():
    async def check(client, controller):
        resp = await client.post("/api/show", json={"screen": "clock", "seconds": 60})
        assert resp.status == 200
        assert controller.shown == [("clock", 60.0)]
    _with_client(check)


def test_api_show_unknown_screen_404():
    async def check(client, controller):
        resp = await client.post("/api/show", json={"screen": "ghost"})
        assert resp.status == 404
        data = await resp.json()
        assert "ghost" in data["error"]
    _with_client(check)


def test_api_show_bad_body():
    async def check(client, controller):
        resp = await client.post("/api/show", json={"nope": 1})
        assert resp.status == 400
        resp = await client.post("/api/show", data="not json",
                                 headers={"Content-Type": "application/json"})
        assert resp.status == 400
        resp = await client.post("/api/show", json={"screen": "a", "seconds": -5})
        assert resp.status == 400
    _with_client(check)


def test_api_rotation_pause_resume():
    async def check(client, controller):
        resp = await client.post("/api/rotation", json={"enabled": False})
        assert resp.status == 200
        assert (await resp.json()) == {"ok": True, "enabled": False}
        assert controller.rotation_enabled is False
        resp = await client.post("/api/rotation", json={"enabled": True})
        assert resp.status == 200
        assert controller.rotation_enabled is True
        assert controller.rotation_calls == [False, True]
        resp = await client.post("/api/rotation", json={})
        assert resp.status == 400
    _with_client(check)


def test_preview_png():
    async def check(client, controller):
        resp = await client.get("/preview/a.png")
        assert resp.status == 200
        assert resp.content_type == "image/png"
        img = Image.open(io.BytesIO(await resp.read()))
        assert img.size == (STRIP_W, STRIP_H)
    _with_client(check)


def test_preview_scale():
    async def check(client, controller):
        resp = await client.get("/preview/a.png?scale=2")
        assert resp.status == 200
        img = Image.open(io.BytesIO(await resp.read()))
        assert img.size == (STRIP_W * 2, STRIP_H * 2)
    _with_client(check)


def test_preview_unknown_screen_404():
    async def check(client, controller):
        resp = await client.get("/preview/ghost.png")
        assert resp.status == 404
    _with_client(check)
