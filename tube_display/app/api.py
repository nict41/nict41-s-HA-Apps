"""HTTP API on 0.0.0.0:8098 (aiohttp).

- GET  /api/status                 current screen, rotation, last error, clock state
- POST /api/show    {"screen": "doorbell", "seconds": 30}
- POST /api/rotation {"enabled": false}
- GET  /preview/<screen>.png[?scale=2]
"""

from __future__ import annotations

import io
import logging

from aiohttp import web

_LOG = logging.getLogger(__name__)


def create_app(controller) -> web.Application:
    app = web.Application()
    app["controller"] = controller

    async def api_status(request: web.Request) -> web.Response:
        return web.json_response(controller.status())

    async def api_show(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "body must be JSON"}, status=400)
        if not isinstance(body, dict) or "screen" not in body:
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
        try:
            controller.show_screen(name, seconds)
        except KeyError:
            return web.json_response(
                {"error": f"unknown screen {name!r}", "screens": controller.status()["screens"]},
                status=404)
        return web.json_response({"ok": True, "screen": name, "seconds": seconds})

    async def api_rotation(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "body must be JSON"}, status=400)
        if not isinstance(body, dict) or "enabled" not in body:
            return web.json_response({"error": "JSON body with an 'enabled' field required"}, status=400)
        enabled = bool(body["enabled"])
        controller.set_rotation_enabled(enabled)
        return web.json_response({"ok": True, "enabled": enabled})

    async def preview(request: web.Request) -> web.Response:
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
            strip = strip.resize((strip.width * scale, strip.height * scale), 0)
        buf = io.BytesIO()
        strip.save(buf, format="PNG")
        return web.Response(body=buf.getvalue(), content_type="image/png")

    app.router.add_get("/api/status", api_status)
    app.router.add_post("/api/show", api_show)
    app.router.add_post("/api/rotation", api_rotation)
    app.router.add_get("/preview/{screen}.png", preview)
    app.router.add_get("/preview/{screen}", preview)
    return app
