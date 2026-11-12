"""HTTP API on 0.0.0.0:8098 (aiohttp).

- GET  /api/status                 first clock's status + {"clocks": {name: status}}
- POST /api/show    {"screen": "doorbell", "seconds": 30, "clock": optional}
- POST /api/rotation {"enabled": false, "clock": optional}
- POST /api/flash   {"message": "...", "seconds": 10, "icon", "color", "clock"}
- POST /api/pause   {"seconds": optional, "clock": optional}
- POST /api/resume  {"clock": optional}
- POST /api/next    {"clock": optional}
- GET  /preview/<screen>.png[?scale=2]
- GET  /firmware/{name}            a firmware file, when a firmware provider is wired

Every POST accepts an optional "clock": the name of one clock. Omitted, the
command applies to every clock; unknown -> 404.
"""

from __future__ import annotations

import io
import logging

from aiohttp import web
from PIL import Image

_LOG = logging.getLogger(__name__)

MAX_MESSAGE_LEN = 255
MAX_FLASH_SECONDS = 3600


def create_app(controllers, firmware=None) -> web.Application:
    """`controllers`: a list of controllers (one per clock) or a single controller
    (the legacy call). `firmware`: optional object with file_path(name) -> path."""
    if not isinstance(controllers, (list, tuple)):
        controllers = [controllers]
    controllers = list(controllers)
    app = web.Application()
    app["controller"] = controllers[0] if controllers else None
    app["controllers"] = {c.name: c for c in controllers}
    app["firmware"] = firmware

    def _resolve_clock(body: dict) -> tuple[list, web.Response | None]:
        """The controllers a POST applies to: the named clock, or all of them.
        Returns (controllers, error_response)."""
        name = body.get("clock")
        if name is None:
            if not controllers:
                return [], web.json_response({"error": "no clocks configured"}, status=404)
            return controllers, None
        name = str(name)
        ctl = app["controllers"].get(name)
        if ctl is None:
            return [], web.json_response(
                {"error": f"unknown clock {name!r}", "clocks": sorted(app["controllers"])},
                status=404)
        return [ctl], None

    async def _json_body(request: web.Request) -> tuple[dict | None, web.Response | None]:
        try:
            body = await request.json()
        except Exception:
            return None, web.json_response({"error": "body must be JSON"}, status=400)
        if not isinstance(body, dict):
            return None, web.json_response({"error": "body must be a JSON object"}, status=400)
        return body, None

    async def api_status(request: web.Request) -> web.Response:
        first = controllers[0] if controllers else None
        status = first.status() if first is not None else {}
        status["clocks"] = {c.name: c.status() for c in controllers}
        return web.json_response(status)

    async def api_show(request: web.Request) -> web.Response:
        body, err = await _json_body(request)
        if err is not None:
            return err
        if "screen" not in body:
            return web.json_response({"error": "JSON body with a 'screen' field required"}, status=400)
        name = str(body["screen"])
        seconds = body.get("seconds")
        if seconds is not None:
            try:
                seconds = float(seconds)
                if seconds <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                return web.json_response({"error": "'seconds' must be a positive number"}, status=400)
        targets, err = _resolve_clock(body)
        if err is not None:
            return err
        for ctl in targets:
            try:
                ctl.show_screen(name, seconds)
            except KeyError:
                return web.json_response(
                    {"error": f"unknown screen {name!r}", "screens": ctl.status()["screens"]},
                    status=404)
        resp = {"ok": True, "screen": name, "seconds": seconds}
        if body.get("clock") is not None:
            resp["clock"] = body["clock"]
        return web.json_response(resp)

    async def api_rotation(request: web.Request) -> web.Response:
        body, err = await _json_body(request)
        if err is not None:
            return err
        if "enabled" not in body:
            return web.json_response({"error": "JSON body with an 'enabled' field required"}, status=400)
        enabled = bool(body["enabled"])
        targets, err = _resolve_clock(body)
        if err is not None:
            return err
        for ctl in targets:
            ctl.set_rotation_enabled(enabled)
        resp = {"ok": True, "enabled": enabled}
        if body.get("clock") is not None:
            resp["clock"] = body["clock"]
        return web.json_response(resp)

    async def api_flash(request: web.Request) -> web.Response:
        body, err = await _json_body(request)
        if err is not None:
            return err
        message = body.get("message")
        if not isinstance(message, str) or not message.strip():
            return web.json_response({"error": "'message' must be a non-empty string"}, status=400)
        if len(message) > MAX_MESSAGE_LEN:
            return web.json_response(
                {"error": f"'message' must be at most {MAX_MESSAGE_LEN} characters"}, status=400)
        seconds = body.get("seconds", 10)
        try:
            seconds = float(seconds)
            if seconds <= 0:
                raise ValueError
            if seconds > MAX_FLASH_SECONDS:
                raise ValueError
        except (TypeError, ValueError):
            return web.json_response(
                {"error": f"'seconds' must be a number > 0 and <= {MAX_FLASH_SECONDS}"}, status=400)
        icon = body.get("icon")
        if icon is not None and (not isinstance(icon, str) or not icon.startswith("mdi:")):
            return web.json_response({"error": "'icon' must be an 'mdi:...' string"}, status=400)
        color = body.get("color")
        if color is not None and (not isinstance(color, str) or not color.startswith("#")):
            return web.json_response({"error": "'color' must be a '#RRGGBB' string"}, status=400)
        targets, err = _resolve_clock(body)
        if err is not None:
            return err
        for ctl in targets:
            ctl.flash(message, seconds=seconds, icon=icon, color=color)
        resp = {"ok": True, "message": message, "seconds": seconds, "icon": icon, "color": color}
        if body.get("clock") is not None:
            resp["clock"] = body["clock"]
        return web.json_response(resp)

    async def api_pause(request: web.Request) -> web.Response:
        body, err = await _json_body(request)
        if err is not None:
            return err
        seconds = body.get("seconds")
        if seconds is not None:
            try:
                seconds = float(seconds)
                if seconds <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                return web.json_response({"error": "'seconds' must be a positive number"}, status=400)
        targets, err = _resolve_clock(body)
        if err is not None:
            return err
        for ctl in targets:
            ctl.pause(seconds)
        resp = {"ok": True, "seconds": seconds}
        if body.get("clock") is not None:
            resp["clock"] = body["clock"]
        return web.json_response(resp)

    async def api_resume(request: web.Request) -> web.Response:
        body, err = await _json_body(request)
        if err is not None:
            return err
        targets, err = _resolve_clock(body)
        if err is not None:
            return err
        for ctl in targets:
            ctl.resume()
        resp = {"ok": True}
        if body.get("clock") is not None:
            resp["clock"] = body["clock"]
        return web.json_response(resp)

    async def api_next(request: web.Request) -> web.Response:
        body, err = await _json_body(request)
        if err is not None:
            return err
        targets, err = _resolve_clock(body)
        if err is not None:
            return err
        for ctl in targets:
            ctl.next_screen()
        resp = {"ok": True}
        if body.get("clock") is not None:
            resp["clock"] = body["clock"]
        return web.json_response(resp)

    async def preview(request: web.Request) -> web.Response:
        controller = app["controller"]
        if controller is None:
            return web.json_response({"error": "no clocks configured"}, status=404)
        name = request.match_info["screen"]
        if name.endswith(".png"):
            name = name[:-4]
        scale = request.query.get("scale", "1")
        try:
            scale = max(1, min(8, int(scale)))
        except ValueError:
            scale = 1
        try:
            strip = await controller.render_preview(name)
        except KeyError:
            return web.json_response(
                {"error": f"unknown screen {name!r}", "screens": controller.status()["screens"]},
                status=404)
        if scale != 1:
            strip = strip.resize((strip.width * scale, strip.height * scale), Image.Resampling.NEAREST)
        buf = io.BytesIO()
        strip.save(buf, format="PNG")
        return web.Response(body=buf.getvalue(), content_type="image/png")

    async def firmware_file(request: web.Request) -> web.Response:
        firmware = app["firmware"]
        name = request.match_info["name"]
        path = firmware.file_path(name) if firmware is not None else None
        if path is None:
            return web.json_response({"error": f"no firmware file {name!r}"}, status=404)
        return web.FileResponse(path, headers={
            "Content-Type": "application/octet-stream",
            "Content-Disposition": f'attachment; filename="{name}"',
        })

    app.router.add_get("/api/status", api_status)
    app.router.add_post("/api/show", api_show)
    app.router.add_post("/api/rotation", api_rotation)
    app.router.add_post("/api/flash", api_flash)
    app.router.add_post("/api/pause", api_pause)
    app.router.add_post("/api/resume", api_resume)
    app.router.add_post("/api/next", api_next)
    app.router.add_get("/preview/{screen}.png", preview)
    app.router.add_get("/preview/{screen}", preview)
    app.router.add_get("/firmware/{name}", firmware_file)
    return app
