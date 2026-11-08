"""Screen editor HTTP API on 0.0.0.0:8099 (aiohttp), reached through HA ingress.

Only the ingress proxy (remote address 172.30.32.2) is accepted; anything else
gets 403. Ingress already requires the HA login. The page is served under the
X-Ingress-Path prefix, so it uses relative URLs only and this module mounts
everything at the root of its own site.

- GET  /                     the editor page (editor_static/index.html)
- GET  /editor.js, /editor.css
- GET  api/config            {"yaml", "data", "mtime", "errors"}
- POST api/validate          {"data": {...}} -> {"errors": [...]}
- POST api/preview           {"screen": {...}, "tube": optional int} -> PNG
- POST api/template          {"template": "..."} -> {"result"|"error"}
- GET  api/entities?q=<text> up to 50 matching entities (cached ~10 s)
- POST api/save              {"data": {...}, "mtime": ...} -> round-trip write
- POST api/save_yaml         {"yaml": "...", "mtime": ...} -> raw write

The 8098 API (api.py) is untouched and has no write routes.
"""

from __future__ import annotations

import io
import logging
import os
import time
from pathlib import Path

import yaml
from aiohttp import web

from render import IMAGE_EXTS, safe_image_name
from screens import ScreensError, parse_screens

_LOG = logging.getLogger(__name__)

INGRESS_PEER = "172.30.32.2"   # the HA ingress proxy's address inside the add-on network
INGRESS_PORT = 8099
STATIC_DIR = Path(__file__).resolve().parent / "editor_static"

# Tests run through aiohttp's test client, whose peer is "127.0.0.1"; allow it
# only when the caller explicitly opts in (see create_app(test_allow_local=True)).
_LOCAL_PEERS = {"127.0.0.1", "::1", "testclient"}

_ENTITY_CACHE_TTL = 10.0


def _peer(request: web.Request) -> str:
    return request.remote or ""


def create_app(controller, ha, screens_file: str, test_allow_local: bool = False) -> web.Application:
    """The editor app. `controller` renders previews, `ha` renders templates and
    lists entities, `screens_file` is the YAML file being edited."""
    app = web.Application(client_max_size=12 * 1024 * 1024)
    app["controller"] = controller
    app["ha"] = ha
    app["screens_file"] = screens_file
    app["allow_local"] = test_allow_local
    app["_entities_cache"] = {"at": 0.0, "q": None, "items": []}

    async def ingress_only(request: web.Request, handler) -> web.Response:
        peer = _peer(request)
        if peer == INGRESS_PEER or (app["allow_local"] and peer in _LOCAL_PEERS):
            return await handler(request)
        _LOG.warning("Editor: refused request from %s", peer or "unknown")
        return web.json_response({"error": "forbidden: not the ingress proxy"}, status=403)

    # ------------------------------------------------------------------
    # static page
    # ------------------------------------------------------------------

    async def index(request: web.Request) -> web.Response:
        return await ingress_only(request, _index)

    async def _index(request: web.Request) -> web.Response:
        # ?v=<file times> on the CSS/JS links so a phone never mixes an old cached file with a new one
        stamp = str(int(max((STATIC_DIR / n).stat().st_mtime for n in ("editor.js", "editor.css"))))
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8").replace("__VERSION__", stamp)
        return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-cache"})

    async def static_file(request: web.Request) -> web.Response:
        name = request.match_info["name"]

        async def handler(req: web.Request) -> web.Response:
            if name not in ("editor.js", "editor.css"):
                raise web.HTTPNotFound()
            ctype = "text/javascript" if name.endswith(".js") else "text/css"
            return web.Response(body=(STATIC_DIR / name).read_bytes(), content_type=ctype,
                                headers={"Cache-Control": "no-cache"})
        return await ingress_only(request, handler)

    # ------------------------------------------------------------------
    # config read / validate
    # ------------------------------------------------------------------

    def _read_config() -> dict:
        path = app["screens_file"]
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
            mtime = os.path.getmtime(path)
        except OSError as err:
            return {"yaml": "", "data": None, "mtime": None,
                    "errors": [f"cannot read {path}: {err}"]}
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as err:
            return {"yaml": text, "data": None, "mtime": mtime,
                    "errors": [f"YAML parse error: {err}"]}
        errors: list[str] = []
        if data is not None:
            try:
                errors = list(parse_screens(data).errors)
            except ScreensError as err:
                errors = [str(err)]
        return {"yaml": text, "data": data, "mtime": mtime, "errors": errors}

    async def api_config(request: web.Request) -> web.Response:
        async def handler(req: web.Request) -> web.Response:
            return web.json_response(_read_config())
        return await ingress_only(request, handler)

    async def api_validate(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "body must be JSON"}, status=400)

        async def handler(req: web.Request) -> web.Response:
            data = body.get("data") if isinstance(body, dict) else None
            if data is None:
                return web.json_response({"errors": ["body needs a 'data' field"]}, status=400)
            try:
                errors = list(parse_screens(data).errors)
            except ScreensError as err:
                errors = [str(err)]
            return web.json_response({"errors": errors})
        return await ingress_only(request, handler)

    # ------------------------------------------------------------------
    # preview / template / entities
    # ------------------------------------------------------------------

    async def api_preview(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "body must be JSON"}, status=400)

        async def handler(req: web.Request) -> web.Response:
            import render as render_mod
            from screens import _parse_screen

            raw = body.get("screen") if isinstance(body, dict) else None
            if not isinstance(raw, dict):
                return web.json_response({"error": "body needs a 'screen' mapping"}, status=400)
            tube = body.get("tube")
            if tube is not None:
                try:
                    tube = int(tube)
                except (TypeError, ValueError):
                    return web.json_response({"error": "'tube' must be an int"}, status=400)
                if not 0 <= tube < 6:
                    return web.json_response({"error": "'tube' must be 0..5"}, status=400)
            # Parse the posted screen with the real parser (raw widget dicts are
            # kept as-is; templates render later, at render time).
            errors: list[str] = []
            screen = _parse_screen("__preview__", raw, errors)
            if screen is None:
                return web.json_response({"error": "invalid screen", "errors": errors}, status=400)
            strip = await render_mod.render_screen(screen, app["controller"]._ctx)
            if tube is not None:
                from rgb565 import TUBE_W
                strip = strip.crop((tube * TUBE_W, 0, (tube + 1) * TUBE_W, strip.height))
            buf = io.BytesIO()
            strip.save(buf, format="PNG")
            return web.Response(body=buf.getvalue(), content_type="image/png")
        return await ingress_only(request, handler)

    async def api_template(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "body must be JSON"}, status=400)

        async def handler(req: web.Request) -> web.Response:
            template = body.get("template") if isinstance(body, dict) else None
            if not isinstance(template, str):
                return web.json_response({"error": "body needs a 'template' string"}, status=400)
            ha = app["ha"]
            if ha is None:
                return web.json_response({"error": "no HA client"}, status=503)
            try:
                result = await ha.render_template(template)
            except Exception as err:
                return web.json_response({"error": str(err)})
            return web.json_response({"result": result})
        return await ingress_only(request, handler)

    async def api_entities(request: web.Request) -> web.Response:
        q = request.query.get("q", "").strip().lower()

        async def handler(req: web.Request) -> web.Response:
            ha = app["ha"]
            if ha is None:
                return web.json_response({"error": "no HA client"}, status=503)
            cache = app["_entities_cache"]
            now = time.monotonic()
            if cache["q"] != q or now - cache["at"] > _ENTITY_CACHE_TTL:
                try:
                    states = await ha._request("GET", "/states")
                except Exception as err:
                    return web.json_response({"error": str(err)}, status=502)
                items = []
                for s in states if isinstance(states, list) else []:
                    eid = s.get("entity_id", "")
                    name = (s.get("attributes") or {}).get("friendly_name", "")
                    if q and q not in eid.lower() and q not in str(name).lower():
                        continue
                    items.append({
                        "entity_id": eid,
                        "name": name,
                        "state": s.get("state"),
                        "unit": (s.get("attributes") or {}).get("unit_of_measurement"),
                    })
                    if len(items) >= 50:
                        break
                cache["q"], cache["at"], cache["items"] = q, now, items
            return web.json_response(cache["items"])
        return await ingress_only(request, handler)

    # ------------------------------------------------------------------
    # save (round-trip, comment preserving) / save_yaml (raw text)
    # ------------------------------------------------------------------

    def _check_mtime(body) -> web.Response | None:
        """None if the mtime matches, else the error response to return."""
        if not isinstance(body, dict) or "mtime" not in body:
            return None  # handled by caller as 400
        try:
            mtime = float(body["mtime"])
        except (TypeError, ValueError):
            return None
        try:
            current = os.path.getmtime(app["screens_file"])
        except OSError as err:
            return web.json_response({"error": f"cannot stat screens file: {err}"}, status=500)
        if current != mtime:
            return web.json_response({"error": "changed on disk"}, status=409)
        return None

    def _atomic_write(text: str) -> float:
        path = app["screens_file"]
        folder = os.path.dirname(path) or "."
        tmp = os.path.join(folder, f".{os.path.basename(path)}.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
        return os.path.getmtime(path)

    def _round_trip_update(old_text: str, data: dict) -> str:
        """Load the existing file with ruamel (comments/layout preserved), update
        rotation/clock/screens in place to match `data`, dump. Multi-line strings
        become literal block scalars (|-)."""
        from ruamel.yaml import YAML
        from ruamel.yaml.scalarstring import LiteralScalarString

        ryaml = YAML()
        ryaml.preserve_quotes = True
        ryaml.indent(mapping=2, sequence=4, offset=2)   # "  - item" under its key, like the example file
        ryaml.width = 4096                              # never re-wrap long template lines
        root = ryaml.load(old_text)
        if not isinstance(root, dict):
            from ruamel.yaml.comments import CommentedMap
            root = CommentedMap()

        def _set_str(value):
            if isinstance(value, str) and "\n" in value:
                return LiteralScalarString(value)
            return value

        def _convert(obj):
            """Plain data -> ruamel-friendly data (literal blocks for multi-line)."""
            if isinstance(obj, dict):
                return {k: _convert(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [_convert(v) for v in obj]
            return _set_str(obj)

        def _merge(old, new):
            """Update `old` (ruamel node) to equal `new`, touching only what changed, so
            untouched screens/tubes keep their comments, flow style and quoting."""
            if isinstance(old, dict) and isinstance(new, dict):
                for k in [k for k in old if k not in new]:
                    del old[k]
                for k, v in new.items():
                    if k in old:
                        merged = _merge(old[k], v)
                        if merged is not old[k]:
                            old[k] = merged
                    else:
                        old[k] = v
                return old
            if isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
                for i, v in enumerate(new):
                    merged = _merge(old[i], v)
                    if merged is not old[i]:
                        old[i] = merged
                return old
            if not isinstance(old, (dict, list)) and old == new:
                return old      # same value: keep the original scalar and its style
            return new

        new = _convert(data)
        for key in ("rotation", "clock", "screens"):
            if key in new:
                root[key] = _merge(root[key], new[key]) if key in root else new[key]
            elif key in root:
                del root[key]
        # any other top-level keys the user had are left alone

        buf = io.StringIO()
        ryaml.dump(root, buf)
        return buf.getvalue()

    async def api_save(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "body must be JSON"}, status=400)

        async def handler(req: web.Request) -> web.Response:
            if not isinstance(body, dict) or not isinstance(body.get("data"), dict):
                return web.json_response({"error": "body needs a 'data' mapping"}, status=400)
            if "mtime" not in body:
                return web.json_response({"error": "body needs an 'mtime'"}, status=400)
            conflict = _check_mtime(body)
            if conflict is not None:
                return conflict
            data = body["data"]
            try:
                errors = list(parse_screens(data).errors)
            except ScreensError as err:
                errors = [str(err)]
            if errors:
                return web.json_response({"error": "invalid screens", "errors": errors}, status=400)
            path = app["screens_file"]
            try:
                with open(path, encoding="utf-8") as f:
                    old_text = f.read()
            except OSError as err:
                return web.json_response({"error": f"cannot read screens file: {err}"}, status=500)
            try:
                new_text = _round_trip_update(old_text, data)
            except Exception as err:
                _LOG.exception("round-trip update failed")
                return web.json_response({"error": f"could not update YAML: {err}"}, status=500)
            mtime = _atomic_write(new_text)
            _LOG.info("Editor saved %s (%d bytes)", path, len(new_text))
            return web.json_response({"ok": True, "mtime": mtime})
        return await ingress_only(request, handler)

    async def api_save_yaml(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "body must be JSON"}, status=400)

        async def handler(req: web.Request) -> web.Response:
            if not isinstance(body, dict) or not isinstance(body.get("yaml"), str):
                return web.json_response({"error": "body needs a 'yaml' string"}, status=400)
            if "mtime" not in body:
                return web.json_response({"error": "body needs an 'mtime'"}, status=400)
            conflict = _check_mtime(body)
            if conflict is not None:
                return conflict
            text = body["yaml"]
            try:
                data = yaml.safe_load(text)
            except yaml.YAMLError as err:
                return web.json_response({"error": "invalid YAML", "errors": [str(err)]}, status=400)
            if data is None:
                return web.json_response({"error": "invalid screens", "errors": ["file is empty"]}, status=400)
            try:
                errors = list(parse_screens(data).errors)
            except ScreensError as err:
                errors = [str(err)]
            if errors:
                return web.json_response({"error": "invalid screens", "errors": errors}, status=400)
            mtime = _atomic_write(text)
            _LOG.info("Editor saved raw YAML to %s (%d bytes)", app["screens_file"], len(text))
            return web.json_response({"ok": True, "mtime": mtime})
        return await ingress_only(request, handler)

    # ------------------------------------------------------------------
    # picture library: <screens file folder>/images, used as source "file:<name>"
    # ------------------------------------------------------------------
    images_dir = os.path.join(os.path.dirname(screens_file) or ".", "images")
    max_image_bytes = 10 * 1024 * 1024

    async def api_images(request: web.Request) -> web.Response:
        async def handler(req: web.Request) -> web.Response:
            names = []
            if os.path.isdir(images_dir):
                names = sorted(n for n in os.listdir(images_dir) if safe_image_name(n))
            return web.json_response({"images": names})
        return await ingress_only(request, handler)

    async def api_image_get(request: web.Request) -> web.Response:
        async def handler(req: web.Request) -> web.Response:
            name = safe_image_name(req.match_info["name"])
            path = os.path.join(images_dir, name) if name else None
            if not path or not os.path.isfile(path):
                raise web.HTTPNotFound()
            return web.FileResponse(path)
        return await ingress_only(request, handler)

    async def api_image_delete(request: web.Request) -> web.Response:
        async def handler(req: web.Request) -> web.Response:
            name = safe_image_name(req.match_info["name"])
            path = os.path.join(images_dir, name) if name else None
            if not path or not os.path.isfile(path):
                raise web.HTTPNotFound()
            os.remove(path)
            return web.json_response({"ok": True})
        return await ingress_only(request, handler)

    async def api_image_upload(request: web.Request) -> web.Response:
        """multipart field "file": a picture, checked with Pillow, saved under a safe name."""
        async def handler(req: web.Request) -> web.Response:
            from PIL import Image
            reader = await req.multipart()
            part = await reader.next()
            while part is not None and part.name != "file":
                part = await reader.next()
            if part is None or not part.filename:
                return web.json_response({"error": "no file"}, status=400)
            from urllib.parse import unquote
            base, ext = os.path.splitext(os.path.basename(unquote(part.filename)))
            ext = ext.lower()
            if ext not in IMAGE_EXTS:
                return web.json_response({"error": "use a .png, .jpg, .gif, .webp or .bmp picture"}, status=400)
            clean = "".join(c if (c.isascii() and c.isalnum()) or c in "-_" else "_" for c in base).strip("_") or "image"
            data = bytearray()
            while True:
                chunk = await part.read_chunk()
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > max_image_bytes:
                    return web.json_response({"error": "picture is over 10 MB"}, status=400)
            try:
                with Image.open(io.BytesIO(bytes(data))) as img:
                    img.verify()
            except Exception:
                return web.json_response({"error": "not a picture Pillow can read"}, status=400)
            os.makedirs(images_dir, exist_ok=True)
            name = clean + ext
            n = 2
            while os.path.exists(os.path.join(images_dir, name)):
                name = f"{clean}-{n}{ext}"
                n += 1
            tmp = os.path.join(images_dir, "." + name + ".tmp")
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, os.path.join(images_dir, name))
            return web.json_response({"ok": True, "name": name, "source": "file:" + name})
        return await ingress_only(request, handler)

    app.router.add_get("/api/images", api_images)
    app.router.add_post("/api/images", api_image_upload)
    app.router.add_get("/api/images/{name}", api_image_get)
    app.router.add_delete("/api/images/{name}", api_image_delete)
    app.router.add_get("/", index)
    app.router.add_get("/{name}", static_file)
    app.router.add_get("/api/config", api_config)
    app.router.add_post("/api/validate", api_validate)
    app.router.add_post("/api/preview", api_preview)
    app.router.add_post("/api/template", api_template)
    app.router.add_get("/api/entities", api_entities)
    app.router.add_post("/api/save", api_save)
    app.router.add_post("/api/save_yaml", api_save_yaml)
    return app
