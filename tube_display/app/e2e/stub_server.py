"""Stub server for the editor's browser e2e tests: the REAL editor app
(editor.create_app(..., test_allow_local=True, controllers=...)) with a stub
Home Assistant and two fake clock controllers, served on 127.0.0.1:<port>.

The stub HA renders templates the way the tests need them: a template that is
just a number passes through, every other {{ ... }} renders to "42" (so ring /
big_number values, clock digits, ... all draw something). It lists a handful of
entities of the domains sensor / weather / media_player / calendar / camera and
returns a short history series for sparkline/graph widgets.

The screens file is a temp copy of e2e/fixtures/screens.yaml, so saves during a
test never touch the repo. The two fake clocks ("main", "kitchen") only
implement what editor.py reads: name, clock.host, status(), rotation_enabled and
set_rotation_enabled().

Usable two ways:
  - as a pytest fixture: `from stub_server import stub_server` (see conftest.py)
  - from the command line for a smoke test:
        python3 stub_server.py [--port 8099] [--screens PATH]
    then fetch / , /api/config and /api/clocks with any HTTP client.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import socket
import sys
import tempfile
import threading
from pathlib import Path

# Make the app modules (editor, render, screens, ...) importable no matter
# where pytest was started from: this file lives in <app>/e2e.
APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "screens.yaml"

# Entities of the domains the editor's pickers / gallery suggestions filter on.
STUB_STATES = [
    {"entity_id": "sensor.cpu_load", "state": "37",
     "attributes": {"friendly_name": "CPU load", "unit_of_measurement": "%",
                    "device_class": "power"}},
    {"entity_id": "sensor.temperature", "state": "21.5",
     "attributes": {"friendly_name": "Living room temperature",
                    "unit_of_measurement": "°C", "device_class": "temperature"}},
    {"entity_id": "sensor.humidity", "state": "48",
     "attributes": {"friendly_name": "Humidity", "unit_of_measurement": "%",
                    "device_class": "humidity"}},
    {"entity_id": "weather.home", "state": "partlycloudy",
     "attributes": {"friendly_name": "Home", "temperature": 19.0, "humidity": 55,
                    "wind_speed": 12.4, "pressure": 1013.2,
                    "forecast": [{"condition": "sunny", "temperature": 22.0},
                                 {"condition": "rain", "temperature": 17.0}]}},
    {"entity_id": "media_player.living_room", "state": "playing",
     "attributes": {"friendly_name": "Living room", "media_title": "Song",
                    "media_artist": "Artist"}},
    {"entity_id": "calendar.home", "state": "on",
     "attributes": {"friendly_name": "Home calendar",
                    "message": "Dentist", "start_time": "2026-11-14T09:00:00"}},
    {"entity_id": "camera.doorbell", "state": "streaming",
     "attributes": {"friendly_name": "Doorbell", "access_token": "x"}},
    {"entity_id": "light.kitchen", "state": "on",
     "attributes": {"friendly_name": "Kitchen light"}},
]

# A short history series: {"state": ..., "last_updated": ...} entries.
STUB_HISTORY = [
    {"state": str(20 + i), "last_updated": f"2026-11-13T{10 + i // 60:02d}:{i % 60:02d}:00+00:00"}
    for i in range(12)
]


class StubHa:
    """Stands in for ha.HaClient: template rendering, /states, history, camera."""

    def __init__(self):
        self.templates: list[str] = []
        self.states = STUB_STATES

    async def render_template(self, template: str) -> str:
        self.templates.append(template)
        if "boom" in template:
            raise ValueError("template exploded")
        stripped = template.strip()
        # a plain number passes through (clock digits, fixed values, ...);
        # every other {{ ... }} / {% ... %} renders to "42"
        if "{{" not in stripped and "{%" not in stripped:
            try:
                float(stripped)
                return stripped
            except ValueError:
                pass
        return "42"

    async def _request(self, method, path, **kwargs):
        assert method == "GET" and path == "/states"
        return self.states

    async def history(self, entity_id: str, hours: float) -> list[dict]:
        return list(STUB_HISTORY)

    async def state(self, entity_id: str) -> dict | None:
        for s in self.states:
            if s["entity_id"] == entity_id:
                return s
        return None

    async def forecast_daily(self, entity_id: str) -> list[dict]:
        for s in self.states:
            if s["entity_id"] == entity_id:
                return list((s.get("attributes") or {}).get("forecast") or [])
        return []

    async def camera_image(self, entity_id: str) -> bytes:
        # a tiny valid PNG (1x1 black) - enough for camera: sources to render
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (8, 8), (10, 10, 10)).save(buf, format="PNG")
        return buf.getvalue()

    async def entity_picture(self, entity_id: str) -> bytes | None:
        return None


class _FakeClock:
    def __init__(self, host):
        self.host = host


class FakeClockController:
    """Just what editor.py reads: name, clock.host, status(), rotation_enabled,
    set_rotation_enabled()."""

    def __init__(self, name: str, host: str, rotation_enabled: bool = True):
        self.name = name
        self.clock = _FakeClock(host)
        self.rotation_enabled = rotation_enabled
        self._status = {
            "current": "main", "live": True, "brightness": 200, "online": True,
            "paused": False, "message": None, "quiet": False,
        }

    def status(self) -> dict:
        return dict(self._status)

    def set_rotation_enabled(self, enabled: bool) -> None:
        self.rotation_enabled = bool(enabled)


def make_controllers() -> dict:
    return {
        "main": FakeClockController("main", "clock-main.local", rotation_enabled=True),
        "kitchen": FakeClockController("kitchen", "clock-kitchen.local", rotation_enabled=False),
    }


def make_screens_file(tmp_dir: str | os.PathLike) -> str:
    """A fresh temp copy of the fixture screens file; returns its path."""
    dst = os.path.join(str(tmp_dir), "screens.yaml")
    shutil.copyfile(FIXTURE, dst)
    return dst


def build_app(screens_file: str, controllers: dict | None = None):
    """The real editor app wired to the stub HA and the fake clocks."""
    import editor as editor_mod

    return editor_mod.create_app(
        None, StubHa(), screens_file,
        test_allow_local=True,
        controllers=controllers if controllers is not None else make_controllers(),
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class StubServer:
    """The editor app running in its own thread with its own event loop.

    `base_url` is http://127.0.0.1:<port>; `screens_file` is the temp copy the
    tests can read back after a save. stop() shuts the loop down.
    """

    def __init__(self, screens_file: str, port: int | None = None,
                 controllers: dict | None = None):
        from aiohttp import web

        self.screens_file = screens_file
        self.controllers = controllers if controllers is not None else make_controllers()
        self.app = build_app(screens_file, self.controllers)
        self.port = port if port is not None else _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._runner: web.AppRunner | None = None
        self._stopping = threading.Event()
        self._ready = threading.Event()
        self._error: BaseException | None = None

    def start(self) -> "StubServer":
        self._thread = threading.Thread(target=self._run, name="stub-server", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=15):
            raise RuntimeError(f"stub server did not start: {self._error!r}")
        if self._error is not None:
            raise RuntimeError(f"stub server failed to start: {self._error!r}")
        return self

    def _run(self) -> None:
        from aiohttp import web

        async def main():
            self._runner = web.AppRunner(self.app)
            await self._runner.setup()
            site = web.TCPSite(self._runner, "127.0.0.1", self.port)
            await site.start()
            self._ready.set()
            # run until stop() sets the event
            while not self._stopping.is_set():
                await asyncio.sleep(0.1)
            await self._runner.cleanup()

        self._stopping = threading.Event()
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(main())
        except BaseException as err:  # noqa: BLE001 - surfaced via _error
            self._error = err
            self._ready.set()
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    def stop(self) -> None:
        if self._loop is not None and self._thread is not None and self._thread.is_alive():
            self._stopping.set()
            self._thread.join(timeout=10)

    def __enter__(self) -> "StubServer":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


def stub_server_fixture(tmp_path_factory):
    """pytest fixture: start a StubServer on a fresh temp screens file.

    Yields the StubServer (base_url, screens_file, controllers); stops it after.
    """
    tmp = tmp_path_factory.mktemp("e2e-stub")
    screens_file = make_screens_file(tmp)
    server = StubServer(screens_file).start()
    yield server
    server.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description="Editor e2e stub server")
    parser.add_argument("--port", type=int, default=None, help="port (default: a free one)")
    parser.add_argument("--screens", default=None,
                        help="screens file to serve (default: a temp copy of the fixture)")
    args = parser.parse_args()

    tmp = tempfile.mkdtemp(prefix="tube-e2e-")
    screens_file = args.screens or make_screens_file(tmp)
    server = StubServer(screens_file, port=args.port).start()
    print(f"stub editor server: {server.base_url}  (screens file: {screens_file})")
    print("Ctrl+C to stop")
    try:
        while True:
            threading.Event().wait(3600)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
