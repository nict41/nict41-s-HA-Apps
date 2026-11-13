"""Screen editor HTTP API on 0.0.0.0:8099 (aiohttp), reached through HA ingress.

Only the ingress proxy (remote address 172.30.32.2) is accepted; anything else
gets 403. Ingress already requires the HA login. The page is served under the
X-Ingress-Path prefix, so it uses relative URLs only and this module mounts
everything at the root of its own site.

- GET  /                     the editor page (editor_static/index.html)
- GET  /editor.js, /editor.css
- GET  api/config            {"yaml", "data", "mtime", "errors", "warnings", "is_example"}
- POST api/validate          {"data": {...}} -> {"errors": [...], "warnings": [...]}
- POST api/preview           {"screen": {...}, "tube": optional int} -> PNG
- GET  api/gallery           {"templates": [{id, name, description, inputs}]}
- POST api/gallery/build     {"id", "values", "name"?} -> {"name", "screen"} (not saved)
- POST api/gallery/preview   {"id", "values"?} -> PNG of the built screen
- POST api/export            {"name", "screen"} -> {"yaml"}
- POST api/import            {"yaml"} -> {"screens": {name: screen}, "errors": [...]}
- GET  api/suggest           {"suggestions": [...]} gallery templates fillable from HA
- POST api/source_image      {"source": "..."} -> PNG of the picture itself (for placing it)
- POST api/template          {"template": "..."} -> {"result"|"error"}
- GET  api/entities?q=<text> up to 50 matching entities (cached ~10 s)
- POST api/save              {"data": {...}, "mtime": ...} -> round-trip write
- POST api/save_yaml         {"yaml": "...", "mtime": ...} -> raw write
- GET  api/history           {"items": [...]} snapshots taken before each save
- GET  api/history/{id}      {"id", "yaml"} one snapshot
- POST api/history/{id}/restore  {"mtime": ...} -> write a snapshot back
- GET  api/clocks            {"clocks": [{"name", "host", "online", "rotation_enabled"}]}
- GET  api/status            {"clocks": {name: controller.status()}}
- POST api/mirror            {"screen": {...}, "clock"?, "seconds"?} -> show on a clock
- DELETE api/mirror          {"clock"?} -> stop mirroring (POST api/mirror/end is an alias)

The 8098 API (api.py) is untouched and has no write routes.

The editor works with no clock configured too (`controller` may be None): previews
then render with the plain REST template renderer and the picture library of the
screens file's folder, never through the app's template cache.
"""

from __future__ import annotations

import io
import logging
import os
import re
import time
from pathlib import Path

import yaml
from aiohttp import web

from render import IMAGE_EXTS, safe_image_name
from screens import ScreensError, example_screens_yaml, parse_screens
from history import SaveHistory
import fonts
import gallery

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


async def _async_none(template: str) -> str:
    """Renderer of last resort when there is no HA client at all (tests)."""
    return ""


def create_app(controller, ha, screens_file: str, test_allow_local: bool = False,
               controllers: dict | None = None) -> web.Application:
    """The editor app. `controller` renders previews (may be None when no clock is
    configured - previews then build their own render context), `ha` renders
    templates and lists entities, `screens_file` is the YAML file being edited.
    `controllers` (optional) maps clock name -> controller for the rotation
    toggle; defaults to the single controller passed in."""
    app = web.Application(client_max_size=12 * 1024 * 1024)
    app["controller"] = controller
    app["controllers"] = (controllers if controllers is not None
                          else {getattr(controller, "name", "main"): controller})
    app["ha"] = ha
    app["screens_file"] = screens_file
    app["history"] = SaveHistory(screens_file)
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
        stamp = str(int(max((STATIC_DIR / n).stat().st_mtime
                            for n in ("editor.js", "editor.css", "when.js"))))
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8").replace("__VERSION__", stamp)
        return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-cache"})

    async def static_file(request: web.Request) -> web.Response:
        name = request.match_info["name"]

        async def handler(req: web.Request) -> web.Response:
            if name not in ("editor.js", "editor.css", "when.js"):
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
                    "errors": [f"cannot read {path}: {err}"], "warnings": [],
                    "is_example": False}
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as err:
            return {"yaml": text, "data": None, "mtime": mtime,
                    "errors": [f"YAML parse error: {err}"], "warnings": [],
                    "is_example": False}
        errors: list[str] = []
        warnings: list[str] = []
        if data is not None:
            try:
                cfg = parse_screens(data)
                errors = list(cfg.errors)
                warnings = list(cfg.warnings)
            except ScreensError as err:
                errors = [str(err)]
        return {"yaml": text, "data": data, "mtime": mtime, "errors": errors,
                "warnings": warnings,
                "is_example": text == example_screens_yaml()}

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
                cfg = parse_screens(data)
                errors = list(cfg.errors)
                warnings = list(cfg.warnings)
            except ScreensError as err:
                errors, warnings = [str(err)], []
            return web.json_response({"errors": errors, "warnings": warnings})
        return await ingress_only(request, handler)

    # ------------------------------------------------------------------
    # preview / template / entities
    # ------------------------------------------------------------------

    async def _preview_response(body) -> web.Response:
        """Render {"screen": {...}, "tube"?, "font"?} to a PNG, exactly like api/preview."""
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
        if screen is None and isinstance(raw.get("tubes"), list):
            # A tube that's still being typed in shouldn't blank the whole preview: draw a
            # red "!" in its place and say why in a header, so the rest stays editable
            from screens import _check_widget
            tubes = []
            for i, w in enumerate(raw["tubes"]):
                werr: list[str] = []
                ok = _check_widget(werr, "__preview__", i, w)
                tubes.append(w if ok and not werr else {"type": "text", "text": "!", "color": "#F44336"})
            fixed = dict(raw, tubes=tubes)
            retry: list[str] = []
            screen = _parse_screen("__preview__", fixed, retry)
        if screen is None:
            return web.json_response({"error": "invalid screen", "errors": errors}, status=400)
        # A copy, so a preview running at the same time doesn't share the box recorder.
        # The preview NEVER uses the controller's (possibly cached) renderer: typing in
        # the editor would create hundreds of websocket subscriptions. With no
        # controller at all, build a context of our own around the plain REST renderer.
        import copy
        import render as render_mod
        ha = app["ha"]
        controller = app["controller"]
        if controller is not None:
            ctx = copy.copy(controller._ctx)
            if ha is not None:
                ctx.renderer = ha.render_template
            if not ctx.images_dir:
                ctx.images_dir = images_dir
        else:
            ctx = render_mod.RenderContext(
                ha.render_template if ha is not None else _async_none,
                session=None, ha=ha)
            ctx.images_dir = images_dir
        # the top-level `font` of the file being edited, if the editor sent it
        if isinstance(body, dict) and "font" in body:
            top_font = body.get("font")
            ctx.default_font = (top_font if isinstance(top_font, str) and top_font
                                and top_font != "default" else None)
        headers: dict[str, str] = {}
        if errors:
            headers["X-Preview-Errors"] = " | ".join(errors)[:900].encode("ascii", "replace").decode()
        # For the designer: where each item of a custom tube (or canvas) ended up
        ctx.record_boxes = {}
        strip = await render_mod.render_screen(screen, ctx)
        import json
        if tube is not None:
            boxes = ctx.record_boxes.get(f"__preview__:tube{tube}")
            from rgb565 import TUBE_W
            strip = strip.crop((tube * TUBE_W, 0, (tube + 1) * TUBE_W, strip.height))
            if boxes is not None:
                headers["X-Item-Boxes"] = json.dumps(boxes)
        elif screen.canvas is not None:
            boxes = ctx.record_boxes.get("__preview__:canvas")
            if boxes is not None:
                headers["X-Item-Boxes"] = json.dumps(boxes)
        buf = io.BytesIO()
        strip.save(buf, format="PNG")
        return web.Response(body=buf.getvalue(), content_type="image/png", headers=headers or None)

    async def api_preview(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "body must be JSON"}, status=400)

        async def handler(req: web.Request) -> web.Response:
            return await _preview_response(body)
        return await ingress_only(request, handler)

    async def api_source_image(request: web.Request) -> web.Response:
        """The picture behind an image source, upright and no bigger than needed, so the
        editor can show it while it is dragged into place (works for cameras and URLs too)."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "body must be JSON"}, status=400)

        async def handler(req: web.Request) -> web.Response:
            import render as render_mod

            source = body.get("source") if isinstance(body, dict) else None
            if not isinstance(source, str) or not source:
                return web.json_response({"error": "body needs a 'source' string"}, status=400)
            controller = app["controller"]
            if controller is not None:
                ctx = controller._ctx
            else:
                import render as render_mod
                ha = app["ha"]
                ctx = render_mod.RenderContext(
                    ha.render_template if ha is not None else _async_none,
                    session=None, ha=ha)
                ctx.images_dir = images_dir
            try:
                if render_mod.is_template(source):
                    source = (await ctx.renderer(source)).strip()
                img = await render_mod._fetch_image(source, ctx)
            except Exception as err:
                return web.json_response({"error": str(err)}, status=400)
            img = img.copy()
            img.thumbnail((render_mod.MAX_SOURCE_PX, render_mod.MAX_SOURCE_PX))
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return web.Response(body=buf.getvalue(), content_type="image/png",
                                headers={"Cache-Control": "no-store"})
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
                    attrs = s.get("attributes") or {}
                    items.append({
                        "entity_id": eid,
                        "name": name,
                        "state": s.get("state"),
                        "unit": attrs.get("unit_of_measurement"),
                        "device_class": attrs.get("device_class"),
                        "domain": eid.split(".", 1)[0] if "." in eid else "",
                    })
                    if len(items) >= 50:
                        break
                cache["q"], cache["at"], cache["items"] = q, now, items
            return web.json_response(cache["items"])
        return await ingress_only(request, handler)

    # ------------------------------------------------------------------
    # gallery: ready-made screens, import/export, entity suggestions
    # ------------------------------------------------------------------

    def _unique_screen_name(name: str, existing) -> str:
        """A screen name that doesn't collide: " 2", " 3", ... appended."""
        name = (name or "").strip() or "screen"
        if name not in existing:
            return name
        n = 2
        while f"{name} {n}" in existing:
            n += 1
        return f"{name} {n}"

    async def api_gallery(request: web.Request) -> web.Response:
        """GET: {"templates": [{id, name, description, inputs}]}."""
        async def handler(req: web.Request) -> web.Response:
            return web.json_response({"templates": gallery.list_templates()})
        return await ingress_only(request, handler)

    async def api_gallery_build(request: web.Request) -> web.Response:
        """POST {"id", "values", "name"?} -> {"name", "screen"} - the built screen,
        NOT saved; the editor adds it to its state and the user saves."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "body must be JSON"}, status=400)

        async def handler(req: web.Request) -> web.Response:
            if not isinstance(body, dict) or not isinstance(body.get("id"), str):
                return web.json_response({"error": "body needs an 'id' string"}, status=400)
            values = body.get("values")
            if values is not None and not isinstance(values, dict):
                return web.json_response({"error": "'values' must be a mapping"}, status=400)
            try:
                screen = gallery.build(body["id"], values or {})
            except ValueError as err:
                return web.json_response({"error": str(err)}, status=400)
            existing = set()
            try:
                with open(app["screens_file"], encoding="utf-8") as f:
                    data = yaml.safe_load(f.read())
                if isinstance(data, dict) and isinstance(data.get("screens"), dict):
                    existing = set(data["screens"])
            except (OSError, yaml.YAMLError):
                pass
            name = _unique_screen_name(str(body.get("name") or ""), existing)
            return web.json_response({"name": name, "screen": screen})
        return await ingress_only(request, handler)

    async def api_gallery_preview(request: web.Request) -> web.Response:
        """POST {"id", "values"?} -> PNG of the built screen. Missing inputs are
        filled with a placeholder entity (domain.example) so a template can be
        previewed before its inputs are chosen."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "body must be JSON"}, status=400)

        async def handler(req: web.Request) -> web.Response:
            if not isinstance(body, dict) or not isinstance(body.get("id"), str):
                return web.json_response({"error": "body needs an 'id' string"}, status=400)
            values = body.get("values")
            if values is not None and not isinstance(values, dict):
                return web.json_response({"error": "'values' must be a mapping"}, status=400)
            values = dict(values or {})
            try:
                template = gallery.get_template(body["id"])
            except KeyError:
                return web.json_response({"error": f"unknown gallery template {body['id']!r}"},
                                         status=400)
            for inp in template["inputs"]:
                key = inp["key"]
                if not str(values.get(key) or "").strip():
                    values[key] = inp.get("domain", "sensor") + ".example"
            try:
                screen = gallery.build(body["id"], values)
            except ValueError as err:
                return web.json_response({"error": str(err)}, status=400)
            return await _preview_response({"screen": screen})
        return await ingress_only(request, handler)

    async def api_export(request: web.Request) -> web.Response:
        """POST {"name", "screen"} -> {"yaml"}: the screen as it would appear under
        `screens:` in the file (same ruamel settings as the round-trip saver)."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "body must be JSON"}, status=400)

        async def handler(req: web.Request) -> web.Response:
            if not isinstance(body, dict):
                return web.json_response({"error": "body must be a JSON mapping"}, status=400)
            name = body.get("name")
            screen = body.get("screen")
            if not isinstance(name, str) or not name.strip():
                return web.json_response({"error": "body needs a 'name' string"}, status=400)
            if not isinstance(screen, dict):
                return web.json_response({"error": "body needs a 'screen' mapping"}, status=400)
            from ruamel.yaml import YAML
            from ruamel.yaml.scalarstring import LiteralScalarString, DoubleQuotedScalarString

            def _set_str(value):
                if isinstance(value, str) and "\n" in value:
                    return LiteralScalarString(value)
                if isinstance(value, str) and re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", value):
                    return DoubleQuotedScalarString(value)
                return value

            def _convert(obj):
                if isinstance(obj, dict):
                    return {k: _convert(v) for k, v in obj.items()}
                if isinstance(obj, list):
                    return [_convert(v) for v in obj]
                return _set_str(obj)

            ryaml = YAML()
            ryaml.preserve_quotes = True
            ryaml.indent(mapping=2, sequence=4, offset=2)
            ryaml.width = 4096
            buf = io.StringIO()
            ryaml.dump({name: _convert(screen)}, buf)
            return web.json_response({"yaml": buf.getvalue()})
        return await ingress_only(request, handler)

    async def api_import(request: web.Request) -> web.Response:
        """POST {"yaml"} -> {"screens": {name: raw screen}, "errors": [...]}.

        Accepts one or more `name: {...}` mappings, or a whole screens file with a
        `screens:` key. Every screen is checked with the real parser; names that
        already exist in the file get " 2", " 3", ... appended."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "body must be JSON"}, status=400)

        async def handler(req: web.Request) -> web.Response:
            from screens import _parse_screen

            text = body.get("yaml") if isinstance(body, dict) else None
            if not isinstance(text, str) or not text.strip():
                return web.json_response({"error": "body needs a 'yaml' string"}, status=400)
            try:
                data = yaml.safe_load(text)
            except yaml.YAMLError as err:
                return web.json_response({"error": "invalid YAML", "errors": [str(err)]},
                                         status=400)
            if not isinstance(data, dict):
                return web.json_response({"error": "not a mapping of screens",
                                          "errors": ["expected `name: {...}` or a `screens:` file"]},
                                         status=400)
            raw_screens = data.get("screens") if isinstance(data.get("screens"), dict) else data
            if not isinstance(raw_screens, dict) or not raw_screens:
                return web.json_response({"error": "no screens found",
                                          "errors": ["expected `name: {...}` or a `screens:` mapping"]},
                                         status=400)
            existing = set()
            try:
                with open(app["screens_file"], encoding="utf-8") as f:
                    cur = yaml.safe_load(f.read())
                if isinstance(cur, dict) and isinstance(cur.get("screens"), dict):
                    existing = set(cur["screens"])
            except (OSError, yaml.YAMLError):
                pass
            out: dict[str, dict] = {}
            errors: list[str] = []
            for name, raw in raw_screens.items():
                if not isinstance(name, str) or not isinstance(raw, dict):
                    errors.append(f"screen {name!r}: not a mapping")
                    continue
                werr: list[str] = []
                wwarn: list[str] = []
                if _parse_screen(name, raw, werr, wwarn) is None:
                    errors.extend(werr or [f"screen '{name}': invalid"])
                    continue
                out[_unique_screen_name(name, existing | set(out))] = raw
            return web.json_response({"screens": out, "errors": errors})
        return await ingress_only(request, handler)

    async def api_suggest(request: web.Request) -> web.Response:
        """GET: {"suggestions": [gallery.suggest(...)]} - gallery templates that can be
        filled automatically from HA's entity list (the setup wizard's step 2)."""
        async def handler(req: web.Request) -> web.Response:
            ha = app["ha"]
            if ha is None:
                return web.json_response({"error": "no HA client"}, status=503)
            try:
                states = await ha._request("GET", "/states")
            except Exception as err:
                return web.json_response({"error": str(err)}, status=502)
            return web.json_response({"suggestions": gallery.suggest(states)})
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
        # keep the current content in the history before overwriting it, so a
        # save (or restore) can be undone; never block a save because of it
        try:
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    app["history"].snapshot(f.read())
        except OSError:
            _LOG.warning("Editor: could not snapshot %s to history", path, exc_info=True)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
        return os.path.getmtime(path)

    def _round_trip_update(old_text: str, data: dict) -> str:
        """Load the existing file with ruamel (comments/layout preserved), update
        rotation/clock/screens/night/clocks in place to match `data`, dump. Multi-line
        strings become literal block scalars (|-)."""
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
            # "23:00" unquoted is a sexagesimal int in YAML 1.1 (PyYAML reads it as 1380),
            # so time-of-day strings must be quoted to survive the round trip
            if isinstance(value, str) and re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", value):
                from ruamel.yaml.scalarstring import DoubleQuotedScalarString
                return DoubleQuotedScalarString(value)
            return value

        def _custom(obj: dict):
            """A custom widget, laid out as a block with one line per item so it stays
            readable (an item with a multi-line string is written as a block instead)."""
            from ruamel.yaml.comments import CommentedMap, CommentedSeq
            out = CommentedMap()
            for k, v in obj.items():
                if k != "items" or not isinstance(v, list):
                    out[k] = _convert(v)
                    continue
                seq = CommentedSeq()
                for it in v:
                    if isinstance(it, dict):
                        node = CommentedMap((ik, _convert(iv)) for ik, iv in it.items())
                        if not any(isinstance(iv, str) and "\n" in iv for iv in it.values()):
                            node.fa.set_flow_style()
                        seq.append(node)
                    else:
                        seq.append(_convert(it))
                out[k] = seq
            return out

        def _is_flow(node) -> bool:
            fa = getattr(node, "fa", None)
            return bool(fa is not None and fa.flow_style())

        def _convert(obj):
            """Plain data -> ruamel-friendly data (literal blocks for multi-line)."""
            if isinstance(obj, dict):
                if obj.get("type") == "custom" and "items" in obj:
                    return _custom(obj)
                return {k: _convert(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [_convert(v) for v in obj]
            return _set_str(obj)

        def _merge(old, new):
            """Update `old` (ruamel node) to equal `new`, touching only what changed, so
            untouched screens/tubes keep their comments, flow style and quoting."""
            if isinstance(old, dict) and isinstance(new, dict):
                if "items" in new and (_is_flow(old) or "items" not in old):
                    return new      # a tube turned into a custom one: don't squeeze it onto one line
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
        for key in ("rotation", "clock", "screens", "night", "clocks", "font"):
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
    # history: snapshots of the screens file taken before every save
    # ------------------------------------------------------------------

    async def api_history(request: web.Request) -> web.Response:
        async def handler(req: web.Request) -> web.Response:
            return web.json_response({"items": app["history"].list()})
        return await ingress_only(request, handler)

    async def api_history_get(request: web.Request) -> web.Response:
        async def handler(req: web.Request) -> web.Response:
            try:
                text = app["history"].read(req.match_info["id"])
            except KeyError:
                return web.json_response({"error": "unknown history id"}, status=404)
            return web.json_response({"id": req.match_info["id"], "yaml": text})
        return await ingress_only(request, handler)

    async def api_history_restore(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "body must be JSON"}, status=400)

        async def handler(req: web.Request) -> web.Response:
            if not isinstance(body, dict) or "mtime" not in body:
                return web.json_response({"error": "body needs an 'mtime'"}, status=400)
            conflict = _check_mtime(body)
            if conflict is not None:
                return conflict
            try:
                text = app["history"].read(req.match_info["id"])
            except KeyError:
                return web.json_response({"error": "unknown history id"}, status=404)
            # the restored text must be valid, exactly like api_save_yaml checks
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
            # _atomic_write snapshots the current file first, so a restore can
            # itself be undone
            mtime = _atomic_write(text)
            _LOG.info("Editor restored %s from history %s", app["screens_file"],
                      req.match_info["id"])
            return web.json_response({"ok": True, "mtime": mtime})
        return await ingress_only(request, handler)

    # ------------------------------------------------------------------
    # picture library: <screens file folder>/images, used as source "file:<name>"
    # ------------------------------------------------------------------
    images_dir = os.path.join(os.path.dirname(screens_file) or ".", "images")
    fonts_dir = os.path.join(os.path.dirname(screens_file) or ".", "fonts")
    fonts.set_user_dir(fonts_dir)
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
            if ext == ".gif":
                # a GIF is also uploaded to the clock for the gif widget (max 600 KB,
                # at most 135x240 - the clock's panel size)
                from clock import GIF_MAX_BYTES
                from rgb565 import TUBE_W, TUBE_H
                if len(data) > GIF_MAX_BYTES:
                    return web.json_response(
                        {"error": f"a GIF for the clock must be at most {GIF_MAX_BYTES // 1024} KB, "
                                  f"this one is {len(data) // 1024} KB"}, status=400)
                try:
                    with Image.open(io.BytesIO(bytes(data))) as img:
                        w, h = img.size
                except Exception:
                    return web.json_response({"error": "not a GIF Pillow can read"}, status=400)
                if w > TUBE_W or h > TUBE_H:
                    return web.json_response(
                        {"error": f"a GIF for the clock must be at most {TUBE_W}x{TUBE_H} pixels, "
                                  f"this one is {w}x{h}"}, status=400)
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

    # ------------------------------------------------------------------
    # font library: <screens file folder>/fonts, used as family "file:<name>"
    # ------------------------------------------------------------------
    max_font_bytes = 3 * 1024 * 1024
    _font_preview_cache: dict[tuple, bytes] = {}

    async def api_fonts(request: web.Request) -> web.Response:
        """GET: {"bundled": [{"id","name"}], "files": [{"name","size"}]}."""
        async def handler(req: web.Request) -> web.Response:
            files = []
            if os.path.isdir(fonts_dir):
                for n in sorted(os.listdir(fonts_dir)):
                    if fonts.safe_font_name(n):
                        try:
                            files.append({"name": n, "size": os.path.getsize(os.path.join(fonts_dir, n))})
                        except OSError:
                            pass
            return web.json_response({"bundled": fonts.bundled_families(), "files": files})
        return await ingress_only(request, handler)

    async def api_font_get(request: web.Request) -> web.Response:
        async def handler(req: web.Request) -> web.Response:
            name = fonts.safe_font_name(req.match_info["name"])
            path = os.path.join(fonts_dir, name) if name else None
            if not path or not os.path.isfile(path):
                raise web.HTTPNotFound()
            return web.FileResponse(path)
        return await ingress_only(request, handler)

    async def api_font_delete(request: web.Request) -> web.Response:
        async def handler(req: web.Request) -> web.Response:
            name = fonts.safe_font_name(req.match_info["name"])
            path = os.path.join(fonts_dir, name) if name else None
            if not path or not os.path.isfile(path):
                raise web.HTTPNotFound()
            os.remove(path)
            fonts._load.cache_clear()
            return web.json_response({"ok": True})
        return await ingress_only(request, handler)

    async def api_font_upload(request: web.Request) -> web.Response:
        """multipart field "file": a .ttf/.otf font (max 3 MB), checked with Pillow,
        saved under a safe name."""
        async def handler(req: web.Request) -> web.Response:
            from PIL import ImageFont
            try:
                reader = await req.multipart()
            except Exception:
                return web.json_response({"error": "body must be multipart form data"}, status=400)
            part = await reader.next()
            while part is not None and part.name != "file":
                part = await reader.next()
            if part is None or not part.filename:
                return web.json_response({"error": "no file"}, status=400)
            from urllib.parse import unquote
            base, ext = os.path.splitext(os.path.basename(unquote(part.filename)))
            ext = ext.lower()
            if ext not in fonts.FONT_EXTS:
                return web.json_response({"error": "use a .ttf or .otf font file"}, status=400)
            clean = "".join(c if (c.isascii() and c.isalnum()) or c in "-_" else "_" for c in base).strip("_") or "font"
            data = bytearray()
            while True:
                chunk = await part.read_chunk()
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > max_font_bytes:
                    return web.json_response({"error": "font is over 3 MB"}, status=400)
            try:
                ImageFont.truetype(io.BytesIO(bytes(data)), 20)
            except Exception:
                return web.json_response({"error": "not a font file Pillow can read"}, status=400)
            os.makedirs(fonts_dir, exist_ok=True)
            name = clean + ext
            n = 2
            while os.path.exists(os.path.join(fonts_dir, name)):
                name = f"{clean}-{n}{ext}"
                n += 1
            tmp = os.path.join(fonts_dir, "." + name + ".tmp")
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, os.path.join(fonts_dir, name))
            fonts._load.cache_clear()
            return web.json_response({"ok": True, "name": name, "family": "file:" + name})
        return await ingress_only(request, handler)

    async def api_font_preview(request: web.Request) -> web.Response:
        """GET ?font=<family id or file:name>&text=<sample>&size=<n>: a small PNG of the
        sample in white on dark, cached per (font, text, size)."""
        async def handler(req: web.Request) -> web.Response:
            from PIL import Image, ImageDraw
            fam = req.query.get("font", "default")
            text = (req.query.get("text") or "Ag 123")[:80]
            try:
                size = max(8, min(120, int(req.query.get("size", "28"))))
            except ValueError:
                size = 28
            key = (fam, text, size)
            png = _font_preview_cache.get(key)
            if png is None:
                img = Image.new("RGB", (min(400, 24 + size * len(text) // 2 + 40), size + 24), (24, 24, 24))
                draw = ImageDraw.Draw(img)
                font = fonts.load_font(size, bold=False, family=fam)
                draw.text((12, 12), text, font=font, fill=(255, 255, 255))
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                png = buf.getvalue()
                if len(_font_preview_cache) > 256:
                    _font_preview_cache.clear()
                _font_preview_cache[key] = png
            return web.Response(body=png, content_type="image/png",
                                headers={"Cache-Control": "max-age=3600"})
        return await ingress_only(request, handler)

    async def api_rotation(request: web.Request) -> web.Response:
        """GET [?clock=<name>]: {"enabled": bool}. POST {"enabled": bool, "clock"?}: on =
        cycle automatically, off = the clock's button changes screens. `clock` picks
        one of the app's controllers (query string for GET, JSON body for POST)."""
        def pick(name):
            """The named clock, else the default one, else the first; None if there are none."""
            if name is not None:
                return app["controllers"].get(str(name))
            if app["controller"] is not None:
                return app["controller"]
            return next(iter(app["controllers"].values()), None)

        async def handler(req: web.Request) -> web.Response:
            name = req.query.get("clock")
            body = {}
            if req.method == "POST":
                body = await req.json()
                if name is None and isinstance(body, dict):
                    name = body.get("clock")
            ctl = pick(name)
            if ctl is None and name is None:
                # No clock configured yet (first run, the setup wizard is showing): not an error
                return web.json_response({"enabled": True, "no_clock": True})
            if ctl is None:
                return web.json_response(
                    {"error": f"unknown clock {name!r}", "clocks": sorted(app["controllers"])},
                    status=404)
            if req.method == "POST":
                ctl.set_rotation_enabled(bool(body.get("enabled")))
            return web.json_response({"enabled": bool(ctl.rotation_enabled)})
        return await ingress_only(request, handler)

    app.router.add_get("/api/rotation", api_rotation)
    app.router.add_post("/api/rotation", api_rotation)

    async def api_clocks(request: web.Request) -> web.Response:
        """GET: {"clocks": [{"name", "host", "online", "rotation_enabled"}]} - one entry
        per configured clock (empty when none)."""
        async def handler(req: web.Request) -> web.Response:
            out = []
            for name, ctl in app["controllers"].items():
                clock = getattr(ctl, "clock", None)
                host = getattr(clock, "host", None) if clock is not None else None
                if host is None:
                    host = getattr(getattr(ctl, "settings", None), "clock_host", "") or ""
                try:
                    online = bool(ctl.status().get("online"))
                except Exception:
                    online = False
                out.append({
                    "name": str(name),
                    "host": str(host),
                    "online": online,
                    "rotation_enabled": bool(getattr(ctl, "rotation_enabled", False)),
                })
            return web.json_response({"clocks": out})
        return await ingress_only(request, handler)

    async def api_status(request: web.Request) -> web.Response:
        """GET: {"clocks": {name: controller.status()}} - live state of every clock
        (empty when none)."""
        async def handler(req: web.Request) -> web.Response:
            out = {}
            for name, ctl in app["controllers"].items():
                try:
                    out[str(name)] = ctl.status()
                except Exception as err:
                    out[str(name)] = {"error": str(err)}
            return web.json_response({"clocks": out})
        return await ingress_only(request, handler)

    app.router.add_get("/api/clocks", api_clocks)
    app.router.add_get("/api/status", api_status)

    # ------------------------------------------------------------------
    # live mirror: show the editor's current screen on a clock ("Show on clock")
    # ------------------------------------------------------------------

    def _mirror_controller(body) -> tuple[web.Response | None, object]:
        """Pick the controller for a mirror request: the "clock" name if given
        (unknown -> 404), else the first one. Returns (error_response, controller)."""
        name = body.get("clock") if isinstance(body, dict) else None
        controllers = app["controllers"]
        if name is not None:
            ctl = controllers.get(str(name))
            if ctl is None:
                return web.json_response(
                    {"error": f"unknown clock {name!r}", "clocks": sorted(controllers)},
                    status=404), None
            return None, ctl
        if not controllers:
            return web.json_response({"error": "no clock configured"}, status=404), None
        first = next(iter(controllers.values()))
        return None, first

    async def api_mirror(request: web.Request) -> web.Response:
        """POST {"screen": {...raw screen dict, as api/preview takes...}, "clock"?,
        "seconds"?} -> parse the screen like api/preview (errors -> 400) and show it
        on that clock (default the first; unknown -> 404) for `seconds`
        (default 120, max 600). Each call replaces the mirrored screen."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "body must be JSON"}, status=400)

        async def handler(req: web.Request) -> web.Response:
            from screens import _parse_screen

            raw = body.get("screen") if isinstance(body, dict) else None
            if not isinstance(raw, dict):
                return web.json_response({"error": "body needs a 'screen' mapping"}, status=400)
            errors: list[str] = []
            screen = _parse_screen("__mirror__", raw, errors)
            if screen is None:
                return web.json_response({"error": "invalid screen", "errors": errors}, status=400)
            if errors:
                # a screen with (non-fatal) errors is still shown, but reported
                _LOG.warning("Mirror screen has errors: %s", errors)
            seconds = body.get("seconds", 120)
            if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds <= 0:
                return web.json_response({"error": "'seconds' must be a positive number"}, status=400)
            seconds = min(float(seconds), 600.0)     # cap at 600 s
            err, ctl = _mirror_controller(body)
            if err is not None:
                return err
            ctl.mirror(screen, seconds=seconds)
            return web.json_response({"ok": True, "clock": getattr(ctl, "name", "main"),
                                      "seconds": seconds})
        return await ingress_only(request, handler)

    async def api_mirror_end(request: web.Request) -> web.Response:
        """DELETE (or POST api/mirror/end, for sendBeacon which cannot DELETE):
        {"clock"?} -> end the mirror on that clock (default the first)."""
        try:
            body = await request.json()
        except Exception:
            body = {}

        async def handler(req: web.Request) -> web.Response:
            err, ctl = _mirror_controller(body if isinstance(body, dict) else {})
            if err is not None:
                return err
            ctl.end_mirror()
            return web.json_response({"ok": True, "clock": getattr(ctl, "name", "main")})
        return await ingress_only(request, handler)

    app.router.add_post("/api/mirror", api_mirror)
    app.router.add_delete("/api/mirror", api_mirror_end)
    app.router.add_post("/api/mirror/end", api_mirror_end)

    async def api_icons(request: web.Request) -> web.Response:
        """GET ?q=<text>: up to 60 Material Design Icon names ("mdi:...") containing the text,
        names starting with it first."""
        async def handler(req: web.Request) -> web.Response:
            import fonts
            q = req.query.get("q", "").strip().lower()
            if q.startswith("mdi:"):
                q = q[4:]
            names = list(fonts._codepoints().keys())
            if q:
                starts = sorted(n for n in names if n.startswith(q))
                contains = sorted(n for n in names if q in n and not n.startswith(q))
                names = starts + contains
            else:
                names = sorted(names)
            return web.json_response({"icons": ["mdi:" + n for n in names[:60]]})
        return await ingress_only(request, handler)

    app.router.add_get("/api/icons", api_icons)
    app.router.add_get("/api/images", api_images)
    app.router.add_post("/api/images", api_image_upload)
    app.router.add_get("/api/images/{name}", api_image_get)
    app.router.add_delete("/api/images/{name}", api_image_delete)
    app.router.add_get("/api/fonts", api_fonts)
    app.router.add_post("/api/fonts", api_font_upload)
    app.router.add_get("/api/fonts/{name}", api_font_get)
    app.router.add_delete("/api/fonts/{name}", api_font_delete)
    app.router.add_get("/api/font_preview", api_font_preview)
    app.router.add_get("/", index)
    app.router.add_get("/{name}", static_file)
    app.router.add_get("/api/config", api_config)
    app.router.add_post("/api/validate", api_validate)
    app.router.add_post("/api/preview", api_preview)
    app.router.add_post("/api/source_image", api_source_image)
    app.router.add_post("/api/template", api_template)
    app.router.add_get("/api/entities", api_entities)
    app.router.add_get("/api/gallery", api_gallery)
    app.router.add_post("/api/gallery/build", api_gallery_build)
    app.router.add_post("/api/gallery/preview", api_gallery_preview)
    app.router.add_post("/api/export", api_export)
    app.router.add_post("/api/import", api_import)
    app.router.add_get("/api/suggest", api_suggest)
    app.router.add_post("/api/save", api_save)
    app.router.add_post("/api/save_yaml", api_save_yaml)
    app.router.add_get("/api/history", api_history)
    app.router.add_get("/api/history/{id}", api_history_get)
    app.router.add_post("/api/history/{id}/restore", api_history_restore)
    return app
