"""Editor API tests (app/editor.py): the ingress site on 8099 with its remote
check, api/config, api/validate, api/preview, api/template, api/entities,
api/save (round-trip, comment preserving) and api/save_yaml.

Uses aiohttp's test client like test_api.py; the app is created with
test_allow_local=True so the test client's 127.0.0.1 peer is accepted. The
403 test builds the app without that flag and fakes the peer.
"""

import asyncio
import io
import os

import yaml
from aiohttp.test_utils import TestClient, TestServer

from conftest import pil, requires_pil

pytestmark = requires_pil

if pil:
    from PIL import Image
    import editor as editor_mod
    from rgb565 import STRIP_W, STRIP_H, TUBE_W, TUBE_H


# The user's real screens file - the main round-trip test case.
USER_SCREENS_YAML = """\
# My Tube Display screens - keep this comment!
rotation: [claude, gpu]
clock: {duration: 60}
screens:
  claude:
    duration: 10
    refresh: 30
    background: "#000000"
    tubes:
      - {type: image, source: "builtin:claude"}
      - {type: ring, value: "{{ states('sensor.claude_usage_nick_pro_session_usage') | float(0) | round(0) | int }}",
         max: 100, label: "Session", unit: "%", unit_newline: true,
         color: "{% set v = states('sensor.claude_usage_nick_pro_session_usage') | float(0) %}{{ '#4CAF50' if v < 50 else '#FFC107' if v < 80 else '#F44336' }}"}
      - type: big_number
        label: "Resets"
        unit_newline: true
        color: "#FFFFFF"
        value: >-
          {%- set r = as_timestamp(states('sensor.x'), 0) -%}
          {{ r | timestamp_custom('%H:%M') if r > 0 else '--:--' }}
        unit: "..."
      - {type: text, text: "line one\\nline two", color: "#FFFFFF"}
      - {type: icon, icon: "mdi:robot", text: "hi", color: "#D97757"}
      - {type: blank}
  gpu:
    strip: {source: "camera:camera.gpu", fit: cover}
"""


class StubHa:
    """Stands in for HaClient: template rendering and the /states list."""

    def __init__(self):
        self.templates = []
        self.states = [
            {"entity_id": "sensor.claude_usage", "state": "42.5",
             "attributes": {"friendly_name": "Claude usage", "unit_of_measurement": "%"}},
            {"entity_id": "sensor.gpu_temp", "state": "61",
             "attributes": {"friendly_name": "GPU temp", "unit_of_measurement": "°C"}},
            {"entity_id": "light.living_room", "state": "on",
             "attributes": {"friendly_name": "Living room"}},
        ]

    async def render_template(self, template: str) -> str:
        self.templates.append(template)
        if "boom" in template:
            raise ValueError("template exploded")
        return "42"

    async def _request(self, method, path, **kwargs):
        assert method == "GET" and path == "/states"
        return self.states


class StubController:
    """Only what editor.py's preview needs: a RenderContext with a stub renderer."""

    def __init__(self):
        import render as render_mod

        async def renderer(t):
            return "42"
        self._ctx = render_mod.RenderContext(renderer, session=None, ha=None)


def _write(tmp_path, text=USER_SCREENS_YAML):
    p = tmp_path / "screens.yaml"
    p.write_text(text, encoding="utf-8")
    return str(p)


def _make_client(tmp_path, text=USER_SCREENS_YAML, allow_local=True):
    screens_file = _write(tmp_path, text)
    app = editor_mod.create_app(StubController(), StubHa(), screens_file,
                                test_allow_local=allow_local)
    return TestClient(TestServer(app)), screens_file


def _with_client(tmp_path, fn, text=USER_SCREENS_YAML, allow_local=True):
    async def wrapper():
        client, screens_file = _make_client(tmp_path, text, allow_local)
        async with client:
            await fn(client, screens_file)
    return asyncio.run(wrapper())


# ----------------------------------------------------------------------
# config / validate
# ----------------------------------------------------------------------

def test_api_config_round_trip(tmp_path):
    async def check(client, screens_file):
        resp = await client.get("/api/config")
        assert resp.status == 200
        data = await resp.json()
        assert data["yaml"] == USER_SCREENS_YAML
        assert data["mtime"] == os.path.getmtime(screens_file)
        assert data["errors"] == []
        assert data["data"]["rotation"] == ["claude", "gpu"]
        assert data["data"]["clock"] == {"duration": 60}
        assert set(data["data"]["screens"]) == {"claude", "gpu"}
    _with_client(tmp_path, check)


def test_api_validate_ok_and_bad(tmp_path):
    async def check(client, screens_file):
        resp = await client.post("/api/validate", json={"data": {
            "rotation": ["a"], "screens": {"a": {"tubes": [{"type": "blank"}] * 6}}}})
        assert resp.status == 200
        assert (await resp.json()) == {"errors": []}
        resp = await client.post("/api/validate", json={"data": {
            "rotation": ["a"], "screens": {"a": {"tubes": [{"type": "nope"}] * 6}}}})
        assert resp.status == 200
        errors = (await resp.json())["errors"]
        assert errors and "unknown widget type" in errors[0]
        resp = await client.post("/api/validate", json={"data": ["not", "a", "map"]})
        assert resp.status == 200
        assert (await resp.json())["errors"]
    _with_client(tmp_path, check)


# ----------------------------------------------------------------------
# preview / template / entities
# ----------------------------------------------------------------------

def test_api_preview_strip_and_tube(tmp_path):
    async def check(client, screens_file):
        cfg = await (await client.get("/api/config")).json()
        screen = cfg["data"]["screens"]["claude"]
        resp = await client.post("/api/preview", json={"screen": screen})
        assert resp.status == 200
        assert resp.content_type == "image/png"
        img = Image.open(io.BytesIO(await resp.read()))
        assert img.size == (STRIP_W, STRIP_H)
        resp = await client.post("/api/preview", json={"screen": screen, "tube": 1})
        assert resp.status == 200
        img = Image.open(io.BytesIO(await resp.read()))
        assert img.size == (TUBE_W, TUBE_H)
        resp = await client.post("/api/preview", json={"screen": screen, "tube": 9})
        assert resp.status == 400
    _with_client(tmp_path, check)


def test_api_template(tmp_path):
    async def check(client, screens_file):
        resp = await client.post("/api/template", json={"template": "{{ 1 + 1 }}"})
        assert resp.status == 200
        assert (await resp.json()) == {"result": "42"}
        resp = await client.post("/api/template", json={"template": "{{ boom }}"})
        assert resp.status == 200
        body = await resp.json()
        assert "error" in body and "exploded" in body["error"]
    _with_client(tmp_path, check)


def test_api_entities_search_and_cache(tmp_path):
    async def check(client, screens_file):
        resp = await client.get("/api/entities?q=gpu")
        assert resp.status == 200
        items = await resp.json()
        assert len(items) == 1
        assert items[0]["entity_id"] == "sensor.gpu_temp"
        assert items[0]["name"] == "GPU temp"
        assert items[0]["state"] == "61"
        assert items[0]["unit"] == "°C"
        resp = await client.get("/api/entities?q=")
        items = await resp.json()
        assert len(items) == 3  # all, capped at 50
        resp = await client.get("/api/entities?q=zzz")
        assert (await resp.json()) == []
    _with_client(tmp_path, check)


# ----------------------------------------------------------------------
# save: round-trip, comments, literal blocks, conflicts
# ----------------------------------------------------------------------

def test_save_keeps_comment_and_semantics(tmp_path):
    async def check(client, screens_file):
        cfg = await (await client.get("/api/config")).json()
        data = cfg["data"]
        resp = await client.post("/api/save", json={"data": data, "mtime": cfg["mtime"]})
        assert resp.status == 200, await resp.text()
        body = await resp.json()
        assert body["ok"] is True
        assert body["mtime"] == os.path.getmtime(screens_file)
        text = open(screens_file, encoding="utf-8").read()
        assert "# My Tube Display screens - keep this comment!" in text
        # re-parsing the saved file gives the same data (no semantic change)
        assert yaml.safe_load(text) == data
    _with_client(tmp_path, check)


def test_save_writes_multiline_template_as_literal_block(tmp_path):
    async def check(client, screens_file):
        cfg = await (await client.get("/api/config")).json()
        data = cfg["data"]
        # change the big_number value to a multi-line template
        data["screens"]["claude"]["tubes"][2]["value"] = "{%- set r = 1 -%}\n{{ r }}"
        resp = await client.post("/api/save", json={"data": data, "mtime": cfg["mtime"]})
        assert resp.status == 200, await resp.text()
        text = open(screens_file, encoding="utf-8").read()
        assert "|-" in text
        assert "{%- set r = 1 -%}" in text
        # and it still parses to the same value
        assert yaml.safe_load(text)["screens"]["claude"]["tubes"][2]["value"] == "{%- set r = 1 -%}\n{{ r }}"
    _with_client(tmp_path, check)


def test_save_409_on_stale_mtime(tmp_path):
    async def check(client, screens_file):
        cfg = await (await client.get("/api/config")).json()
        # the file changes on disk after the page loaded it
        with open(screens_file, "a", encoding="utf-8") as f:
            f.write("\n# edited elsewhere\n")
        resp = await client.post("/api/save", json={"data": cfg["data"], "mtime": cfg["mtime"]})
        assert resp.status == 409
        assert (await resp.json())["error"] == "changed on disk"
    _with_client(tmp_path, check)


def test_save_400_on_invalid_data(tmp_path):
    async def check(client, screens_file):
        cfg = await (await client.get("/api/config")).json()
        bad = {"rotation": ["ghost"], "screens": {"a": {"tubes": [{"type": "blank"}] * 6}}}
        resp = await client.post("/api/save", json={"data": bad, "mtime": cfg["mtime"]})
        assert resp.status == 400
        body = await resp.json()
        assert body["errors"] and "ghost" in body["errors"][0]
        # file untouched
        assert open(screens_file, encoding="utf-8").read() == USER_SCREENS_YAML
    _with_client(tmp_path, check)


def test_save_add_remove_screen(tmp_path):
    async def check(client, screens_file):
        cfg = await (await client.get("/api/config")).json()
        data = cfg["data"]
        data["screens"]["new"] = {"tubes": [{"type": "blank"}] * 6}
        data["rotation"] = ["claude", "new"]
        del data["screens"]["gpu"]
        resp = await client.post("/api/save", json={"data": data, "mtime": cfg["mtime"]})
        assert resp.status == 200, await resp.text()
        saved = yaml.safe_load(open(screens_file, encoding="utf-8").read())
        assert set(saved["screens"]) == {"claude", "new"}
        assert saved["rotation"] == ["claude", "new"]
        assert saved["clock"] == {"duration": 60}
    _with_client(tmp_path, check)


def test_save_yaml_raw(tmp_path):
    async def check(client, screens_file):
        cfg = await (await client.get("/api/config")).json()
        text = USER_SCREENS_YAML.replace("duration: 10", "duration: 12")
        resp = await client.post("/api/save_yaml", json={"yaml": text, "mtime": cfg["mtime"]})
        assert resp.status == 200, await resp.text()
        assert open(screens_file, encoding="utf-8").read() == text
        # invalid YAML -> 400, file untouched
        resp = await client.post("/api/save_yaml", json={"yaml": "rotation: [unclosed", "mtime": (await resp.json())["mtime"]})
        assert resp.status == 400
        # valid YAML but invalid screens -> 400
        resp = await client.post("/api/save_yaml", json={"yaml": "rotation: []\nscreens: {}", "mtime": os.path.getmtime(screens_file)})
        assert resp.status == 400
        assert open(screens_file, encoding="utf-8").read() == text
    _with_client(tmp_path, check)


# ----------------------------------------------------------------------
# ingress remote check
# ----------------------------------------------------------------------

def test_403_from_non_ingress_peer(tmp_path):
    """Without test_allow_local, only 172.30.32.2 is accepted; the test client's
    127.0.0.1 peer gets 403. A faked ingress peer gets through."""
    async def check(client, screens_file):
        resp = await client.get("/api/config")
        assert resp.status == 403
        resp = await client.get("/")
        assert resp.status == 403
    _with_client(tmp_path, check, allow_local=False)


def test_ingress_peer_accepted(tmp_path):
    """Simulate the ingress proxy by patching request.remote to 172.30.32.2."""
    async def wrapper():
        client, screens_file = _make_client(tmp_path, allow_local=False)
        async with client:
            # aiohttp's test server sees 127.0.0.1; monkeypatch the peer check
            orig = editor_mod._peer
            editor_mod._peer = lambda request: "172.30.32.2"
            try:
                resp = await client.get("/api/config")
                assert resp.status == 200
                assert (await resp.json())["data"]["rotation"] == ["claude", "gpu"]
            finally:
                editor_mod._peer = orig
    asyncio.run(wrapper())


# ----------------------------------------------------------------------
# static page
# ----------------------------------------------------------------------

def test_index_and_static_served(tmp_path):
    async def check(client, screens_file):
        resp = await client.get("/")
        assert resp.status == 200
        assert resp.content_type == "text/html"
        html = await resp.text()
        assert "editor.js?v=" in html and "editor.css?v=" in html and "__VERSION__" not in html
        # relative URLs only - no absolute /api or /editor references
        assert 'src="/editor.js"' not in html and 'href="/editor.css"' not in html
        resp = await client.get("/editor.js")
        assert resp.status == 200
        js = await resp.text()
        assert 'api("api/config")' in js
        assert '"/api/' not in js and "'/api/" not in js
        resp = await client.get("/editor.css")
        assert resp.status == 200
        resp = await client.get("/nope.txt")
        assert resp.status == 404
    _with_client(tmp_path, check)



def test_save_keeps_untouched_tubes_as_written(tmp_path):
    async def check(client, screens_file):
        before = open(screens_file, encoding="utf-8").read()
        cfg = await (await client.get("/api/config")).json()
        data = cfg["data"]
        data["screens"]["claude"]["duration"] = 12
        resp = await client.post("/api/save", json={"data": data, "mtime": cfg["mtime"]})
        assert resp.status == 200, await resp.text()
        text = open(screens_file, encoding="utf-8").read()
        assert "duration: 12" in text
        # Nothing else changes: same text once whitespace is collapsed (ruamel puts a
        # flow mapping that was wrapped over several lines back on one line)
        squash = lambda t: " ".join(t.split())
        assert squash(text) == squash(before).replace("duration: 10", "duration: 12", 1)
    _with_client(tmp_path, check)


def _png_bytes(color=(255, 0, 0, 255), size=(20, 30)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGBA", size, color).save(buf, format="PNG")
    return buf.getvalue()


def test_image_upload_list_get_delete(tmp_path):
    async def check(client, screens_file):
        import aiohttp
        form = aiohttp.FormData()
        form.add_field("file", _png_bytes(), filename="My Logo!.png", content_type="image/png")
        resp = await client.post("/api/images", data=form)
        assert resp.status == 200, await resp.text()
        body = await resp.json()
        assert body["name"] == "My_Logo.png" and body["source"] == "file:My_Logo.png"
        images_dir = os.path.join(os.path.dirname(screens_file), "images")
        assert os.path.isfile(os.path.join(images_dir, "My_Logo.png"))
        # same name again gets a new one rather than overwriting
        form = aiohttp.FormData()
        form.add_field("file", _png_bytes(), filename="My Logo!.png", content_type="image/png")
        assert (await (await client.post("/api/images", data=form)).json())["name"] == "My_Logo-2.png"
        names = (await (await client.get("/api/images")).json())["images"]
        assert names == ["My_Logo-2.png", "My_Logo.png"]
        assert (await client.get("/api/images/My_Logo.png")).status == 200
        assert (await client.get("/api/images/..%2Fscreens.yaml")).status == 404
        assert (await client.delete("/api/images/My_Logo.png")).status == 200
        assert not os.path.exists(os.path.join(images_dir, "My_Logo.png"))
    _with_client(tmp_path, check)


def test_image_upload_rejects_non_pictures(tmp_path):
    async def check(client, screens_file):
        import aiohttp
        form = aiohttp.FormData()
        form.add_field("file", b"not a png", filename="x.png", content_type="image/png")
        assert (await client.post("/api/images", data=form)).status == 400
        form = aiohttp.FormData()
        form.add_field("file", _png_bytes(), filename="x.svg", content_type="image/svg+xml")
        assert (await client.post("/api/images", data=form)).status == 400
    _with_client(tmp_path, check)


def test_rotation_toggle(tmp_path):
    async def check(client, screens_file):
        ctl = client.app["controller"]
        ctl.rotation_enabled = True
        ctl.set_rotation_enabled = lambda on: setattr(ctl, "rotation_enabled", on)
        assert (await (await client.get("/api/rotation")).json()) == {"enabled": True}
        resp = await client.post("/api/rotation", json={"enabled": False})
        assert (await resp.json()) == {"enabled": False} and ctl.rotation_enabled is False
    _with_client(tmp_path, check)


def test_preview_with_bad_tube_still_draws(tmp_path):
    async def check(client, screens_file):
        screen = {"tubes": [{"type": "icon", "icon": "train"}] + [{"type": "text", "text": "x"}] * 5}
        resp = await client.post("/api/preview", json={"screen": screen})
        assert resp.status == 200
        assert resp.content_type == "image/png"
        assert "icon" in resp.headers.get("X-Preview-Errors", "")
    _with_client(tmp_path, check)


def test_icon_search(tmp_path, monkeypatch):
    import fonts
    monkeypatch.setattr(fonts, "_codepoints", lambda: {"train": 1, "train-car": 2, "subway-variant": 3, "bus": 4})
    async def check(client, screens_file):
        body = await (await client.get("/api/icons?q=mdi:tra")).json()
        assert body["icons"][:2] == ["mdi:train", "mdi:train-car"]
        assert "mdi:bus" not in body["icons"]
    _with_client(tmp_path, check)


# ----------------------------------------------------------------------
# designer support: item boxes, source pictures, custom tubes in the file
# ----------------------------------------------------------------------

CUSTOM_TUBE = {"type": "custom", "items": [
    {"type": "ring", "x": 67, "y": 60, "radius": 50, "value": "40"},
    {"type": "text", "x": 67, "y": 160, "text": "two\nlines", "size": 16},
    {"type": "text", "x": 67, "y": 200, "text": "one", "size": 16},
]}


def test_preview_returns_item_boxes_for_a_custom_tube(tmp_path):
    async def check(client, screens_file):
        import json
        cfg = await (await client.get("/api/config")).json()
        screen = cfg["data"]["screens"]["claude"]
        screen["tubes"][5] = CUSTOM_TUBE
        resp = await client.post("/api/preview", json={"screen": screen, "tube": 5})
        assert resp.status == 200
        boxes = json.loads(resp.headers["X-Item-Boxes"])
        assert len(boxes) == 3 and boxes[0] == [17, 10, 117, 110]
        # other tubes and the whole strip carry no boxes
        resp = await client.post("/api/preview", json={"screen": screen, "tube": 1})
        assert "X-Item-Boxes" not in resp.headers
        resp = await client.post("/api/preview", json={"screen": screen})
        assert "X-Item-Boxes" not in resp.headers
    _with_client(tmp_path, check)


def test_source_image(tmp_path):
    async def check(client, screens_file):
        resp = await client.post("/api/source_image", json={"source": "builtin:claude"})
        assert resp.status == 200 and resp.content_type == "image/png"
        assert Image.open(io.BytesIO(await resp.read())).size == (TUBE_W, TUBE_H)
        resp = await client.post("/api/source_image", json={"source": "file:missing.png"})
        assert resp.status == 400 and "error" in await resp.json()
        resp = await client.post("/api/source_image", json={})
        assert resp.status == 400
    _with_client(tmp_path, check)


def test_save_custom_tube_is_readable_yaml(tmp_path):
    async def check(client, screens_file):
        cfg = await (await client.get("/api/config")).json()
        data = cfg["data"]
        data["screens"]["claude"]["tubes"][1] = CUSTOM_TUBE      # was a one-line {type: ring, ...}
        resp = await client.post("/api/save", json={"data": data, "mtime": cfg["mtime"]})
        assert resp.status == 200, await resp.text()
        text = open(screens_file, encoding="utf-8").read()
        assert "# My Tube Display screens - keep this comment!" in text
        assert yaml.safe_load(text)["screens"]["claude"]["tubes"][1] == CUSTOM_TUBE
        lines = text.splitlines()
        # one item per line, not the whole tube squeezed onto one
        assert any(ln.strip().startswith("- {type: ring, x: 67") for ln in lines)
        assert any(ln.strip() == "items:" for ln in lines)
        assert max(len(ln) for ln in lines if "type: custom" in ln) < 40
        # saving again with one item moved keeps the layout
        cfg = await (await client.get("/api/config")).json()
        cfg["data"]["screens"]["claude"]["tubes"][1]["items"][2]["y"] = 210
        resp = await client.post("/api/save", json={"data": cfg["data"], "mtime": cfg["mtime"]})
        assert resp.status == 200
        text = open(screens_file, encoding="utf-8").read()
        assert "y: 210" in text and any(ln.strip() == "items:" for ln in text.splitlines())
    _with_client(tmp_path, check)


# ----------------------------------------------------------------------
# no controller (no clocks configured) and preview must not use the cache
# ----------------------------------------------------------------------

def test_preview_works_with_no_controller(tmp_path):
    """With controller=None the editor still previews, using its own render
    context around the plain REST renderer (never the template cache)."""
    screens_file = _write(tmp_path)
    app = editor_mod.create_app(None, StubHa(), screens_file, test_allow_local=True)

    async def check():
        async with TestClient(TestServer(app)) as client:
            cfg = await (await client.get("/api/config")).json()
            screen = cfg["data"]["screens"]["claude"]
            resp = await client.post("/api/preview", json={"screen": screen})
            assert resp.status == 200
            assert resp.content_type == "image/png"
            img = Image.open(io.BytesIO(await resp.read()))
            assert img.size == (STRIP_W, STRIP_H)
            # the preview rendered the template through the REST renderer
            assert any("42" in t or True for t in app["ha"].templates)
    asyncio.run(check())


def test_preview_uses_rest_renderer_not_the_controller_cache(tmp_path):
    """api_preview must set ctx.renderer to ha.render_template even when the
    controller's context uses a (cached) renderer - typing in the editor must not
    create websocket subscriptions."""
    screens_file = _write(tmp_path)

    class CachedController(StubController):
        def __init__(self):
            super().__init__()
            async def cached_renderer(t):
                raise AssertionError("the cache renderer must not be used by previews")
            self._ctx.renderer = cached_renderer

    app = editor_mod.create_app(CachedController(), StubHa(), screens_file, test_allow_local=True)

    async def check():
        async with TestClient(TestServer(app)) as client:
            cfg = await (await client.get("/api/config")).json()
            screen = cfg["data"]["screens"]["claude"]
            resp = await client.post("/api/preview", json={"screen": screen})
            assert resp.status == 200
            assert resp.content_type == "image/png"
    asyncio.run(check())


# ----------------------------------------------------------------------
# saving the night: and clocks: blocks (round-trip, comment preserving)
# ----------------------------------------------------------------------

NIGHT = {"brightness_day": 255, "brightness_night": 50, "bright_above": 3,
         "dim_below": -6, "dim_screens": 0.6}


def test_save_night_added_changed_removed(tmp_path):
    async def check(client, screens_file):
        cfg = await (await client.get("/api/config")).json()
        data = cfg["data"]
        assert "night" not in data
        # added
        data["night"] = dict(NIGHT)
        resp = await client.post("/api/save", json={"data": data, "mtime": cfg["mtime"]})
        assert resp.status == 200, await resp.text()
        text = open(screens_file, encoding="utf-8").read()
        assert "# My Tube Display screens - keep this comment!" in text
        saved = yaml.safe_load(text)
        assert saved["night"] == NIGHT
        # changed (a comment elsewhere in the file survives)
        cfg = await (await client.get("/api/config")).json()
        cfg["data"]["night"]["brightness_night"] = 30
        cfg["data"]["night"]["quiet"] = {"from": "23:00", "to": "07:00", "mode": "blank"}
        resp = await client.post("/api/save", json={"data": cfg["data"], "mtime": cfg["mtime"]})
        assert resp.status == 200, await resp.text()
        text = open(screens_file, encoding="utf-8").read()
        assert "# My Tube Display screens - keep this comment!" in text
        saved = yaml.safe_load(text)
        assert saved["night"]["brightness_night"] == 30
        assert saved["night"]["quiet"] == {"from": "23:00", "to": "07:00", "mode": "blank"}
        assert saved["night"]["dim_screens"] == 0.6
        # removed again
        cfg = await (await client.get("/api/config")).json()
        del cfg["data"]["night"]
        resp = await client.post("/api/save", json={"data": cfg["data"], "mtime": cfg["mtime"]})
        assert resp.status == 200, await resp.text()
        text = open(screens_file, encoding="utf-8").read()
        assert "night" not in yaml.safe_load(text)
        assert "# My Tube Display screens - keep this comment!" in text
    _with_client(tmp_path, check)


def test_save_clocks_override_added_and_removed(tmp_path):
    async def check(client, screens_file):
        cfg = await (await client.get("/api/config")).json()
        data = cfg["data"]
        assert "clocks" not in data
        # added: a per-clock rotation + clock duration
        data["clocks"] = {"kitchen": {"rotation": ["claude", "clock"], "clock": {"duration": 30}}}
        resp = await client.post("/api/save", json={"data": data, "mtime": cfg["mtime"]})
        assert resp.status == 200, await resp.text()
        text = open(screens_file, encoding="utf-8").read()
        assert "# My Tube Display screens - keep this comment!" in text
        saved = yaml.safe_load(text)
        assert saved["clocks"] == {"kitchen": {"rotation": ["claude", "clock"], "clock": {"duration": 30}}}
        # changed in place
        cfg = await (await client.get("/api/config")).json()
        cfg["data"]["clocks"]["kitchen"]["clock"]["duration"] = 45
        resp = await client.post("/api/save", json={"data": cfg["data"], "mtime": cfg["mtime"]})
        assert resp.status == 200, await resp.text()
        saved = yaml.safe_load(open(screens_file, encoding="utf-8").read())
        assert saved["clocks"]["kitchen"]["clock"]["duration"] == 45
        assert saved["clocks"]["kitchen"]["rotation"] == ["claude", "clock"]
        # removed (the whole clocks: block goes when the last override is deleted)
        cfg = await (await client.get("/api/config")).json()
        del cfg["data"]["clocks"]["kitchen"]
        del cfg["data"]["clocks"]
        resp = await client.post("/api/save", json={"data": cfg["data"], "mtime": cfg["mtime"]})
        assert resp.status == 200, await resp.text()
        assert "clocks" not in yaml.safe_load(open(screens_file, encoding="utf-8").read())
    _with_client(tmp_path, check)


# ----------------------------------------------------------------------
# api/clocks and api/status
# ----------------------------------------------------------------------

class FakeClock:
    def __init__(self, host):
        self.host = host


class FakeController:
    """Just what api/clocks and api/status read: name, clock.host, status(),
    rotation_enabled."""

    def __init__(self, name, host, status=None, rotation_enabled=True):
        self.name = name
        self.clock = FakeClock(host)
        self.rotation_enabled = rotation_enabled
        self._status = status if status is not None else {
            "current": "claude", "live": True, "brightness": 128, "online": True,
            "paused": False, "message": None, "quiet": False,
        }

    def status(self):
        return dict(self._status)


def _make_client_controllers(tmp_path, controllers):
    screens_file = _write(tmp_path)
    app = editor_mod.create_app(None, StubHa(), screens_file, test_allow_local=True,
                                controllers=controllers)
    return TestClient(TestServer(app)), screens_file


def test_api_clocks_and_status_with_fake_controllers(tmp_path):
    async def wrapper():
        controllers = {
            "main": FakeController("main", "clock-main.local", rotation_enabled=True),
            "kitchen": FakeController("kitchen", "clock-kitchen.local",
                                      status={"current": "clock", "live": False, "brightness": None,
                                              "online": False, "paused": True, "message": "OTA",
                                              "quiet": False},
                                      rotation_enabled=False),
        }
        client, screens_file = _make_client_controllers(tmp_path, controllers)
        async with client:
            body = await (await client.get("/api/clocks")).json()
            assert body == {"clocks": [
                {"name": "main", "host": "clock-main.local", "online": True, "rotation_enabled": True},
                {"name": "kitchen", "host": "clock-kitchen.local", "online": False, "rotation_enabled": False},
            ]}
            body = await (await client.get("/api/status")).json()
            assert set(body["clocks"]) == {"main", "kitchen"}
            assert body["clocks"]["main"]["current"] == "claude"
            assert body["clocks"]["main"]["live"] is True
            assert body["clocks"]["main"]["brightness"] == 128
            assert body["clocks"]["kitchen"]["online"] is False
            assert body["clocks"]["kitchen"]["paused"] is True
    asyncio.run(wrapper())


def test_api_clocks_and_status_with_no_controllers(tmp_path):
    async def wrapper():
        client, screens_file = _make_client_controllers(tmp_path, {})
        async with client:
            assert (await (await client.get("/api/clocks")).json()) == {"clocks": []}
            assert (await (await client.get("/api/status")).json()) == {"clocks": {}}
    asyncio.run(wrapper())


# ----------------------------------------------------------------------
# live mirror: POST/DELETE api/mirror, POST api/mirror/end
# ----------------------------------------------------------------------

class MirrorController:
    """Records mirror()/end_mirror() calls like the real Controller."""

    def __init__(self, name="main"):
        self.name = name
        self.clock = FakeClock("clock-" + name + ".local")
        self.rotation_enabled = True
        self.mirrors = []       # (screen, seconds)
        self.ends = 0

    def mirror(self, screen, seconds=120):
        self.mirrors.append((screen, seconds))

    def end_mirror(self):
        self.ends += 1

    def status(self):
        return {"current": "x", "mirroring": bool(self.mirrors), "online": True}


def _mirror_client(tmp_path, controllers=None):
    screens_file = _write(tmp_path)
    if controllers is None:
        controllers = {"main": MirrorController("main")}
    app = editor_mod.create_app(None, StubHa(), screens_file, test_allow_local=True,
                                controllers=controllers)
    return TestClient(TestServer(app)), screens_file, controllers


GOOD_SCREEN = {"tubes": [{"type": "text", "text": "hi"}] * 6}


def test_api_mirror_post_shows_screen_on_default_clock(tmp_path):
    async def wrapper():
        client, screens_file, controllers = _mirror_client(tmp_path)
        async with client:
            resp = await client.post("/api/mirror", json={"screen": GOOD_SCREEN})
            assert resp.status == 200, await resp.text()
            body = await resp.json()
            assert body["ok"] is True and body["clock"] == "main" and body["seconds"] == 120
            ctl = controllers["main"]
            assert len(ctl.mirrors) == 1
            screen, seconds = ctl.mirrors[0]
            assert screen.name == "__mirror__" and seconds == 120.0
            assert screen.tubes[0]["text"] == "hi"
            # each call replaces the screen and restarts the timer
            resp = await client.post("/api/mirror", json={"screen": GOOD_SCREEN, "seconds": 600})
            assert resp.status == 200
            assert (await resp.json())["seconds"] == 600.0
            assert len(ctl.mirrors) == 2 and ctl.mirrors[1][1] == 600.0
    asyncio.run(wrapper())


def test_api_mirror_seconds_capped_and_validated(tmp_path):
    async def wrapper():
        client, screens_file, controllers = _mirror_client(tmp_path)
        async with client:
            resp = await client.post("/api/mirror", json={"screen": GOOD_SCREEN, "seconds": 99999})
            assert resp.status == 200
            assert (await resp.json())["seconds"] == 600.0     # capped at 600
            assert controllers["main"].mirrors[-1][1] == 600.0
            resp = await client.post("/api/mirror", json={"screen": GOOD_SCREEN, "seconds": -5})
            assert resp.status == 400
            resp = await client.post("/api/mirror", json={"screen": GOOD_SCREEN, "seconds": "soon"})
            assert resp.status == 400
    asyncio.run(wrapper())


def test_api_mirror_invalid_screen_is_400_with_errors(tmp_path):
    async def wrapper():
        client, screens_file, controllers = _mirror_client(tmp_path)
        async with client:
            resp = await client.post("/api/mirror", json={"screen": {"tubes": [{"type": "nope"}] * 6}})
            assert resp.status == 400
            body = await resp.json()
            assert "errors" in body and any("unknown widget type" in e for e in body["errors"])
            assert controllers["main"].mirrors == []           # nothing mirrored
            resp = await client.post("/api/mirror", json={})
            assert resp.status == 400
            resp = await client.post("/api/mirror", data="not json",
                                     headers={"Content-Type": "application/json"})
            assert resp.status == 400
    asyncio.run(wrapper())


def test_api_mirror_unknown_clock_is_404_and_named_clock_used(tmp_path):
    async def wrapper():
        controllers = {"main": MirrorController("main"), "kitchen": MirrorController("kitchen")}
        client, screens_file, controllers = _mirror_client(tmp_path, controllers)
        async with client:
            resp = await client.post("/api/mirror", json={"screen": GOOD_SCREEN, "clock": "ghost"})
            assert resp.status == 404
            assert "ghost" in (await resp.json())["error"]
            resp = await client.post("/api/mirror", json={"screen": GOOD_SCREEN, "clock": "kitchen"})
            assert resp.status == 200 and (await resp.json())["clock"] == "kitchen"
            assert controllers["kitchen"].mirrors and not controllers["main"].mirrors
    asyncio.run(wrapper())


def test_api_mirror_delete_and_end_alias(tmp_path):
    async def wrapper():
        client, screens_file, controllers = _mirror_client(tmp_path)
        async with client:
            await client.post("/api/mirror", json={"screen": GOOD_SCREEN})
            resp = await client.delete("/api/mirror", json={})
            assert resp.status == 200 and (await resp.json())["ok"] is True
            assert controllers["main"].ends == 1
            # the POST alias (for sendBeacon, which cannot DELETE)
            await client.post("/api/mirror", json={"screen": GOOD_SCREEN})
            resp = await client.post("/api/mirror/end", json={})
            assert resp.status == 200
            assert controllers["main"].ends == 2
            # unknown clock -> 404
            resp = await client.delete("/api/mirror", json={"clock": "ghost"})
            assert resp.status == 404
    asyncio.run(wrapper())


def test_api_mirror_no_clocks_is_404(tmp_path):
    async def wrapper():
        client, screens_file, controllers = _mirror_client(tmp_path, controllers={})
        async with client:
            resp = await client.post("/api/mirror", json={"screen": GOOD_SCREEN})
            assert resp.status == 404
            resp = await client.delete("/api/mirror", json={})
            assert resp.status == 404
    asyncio.run(wrapper())


def test_api_mirror_ingress_only(tmp_path):
    """Without test_allow_local the mirror routes are refused like the others."""
    async def wrapper():
        screens_file = _write(tmp_path)
        app = editor_mod.create_app(None, StubHa(), screens_file, test_allow_local=False,
                                    controllers={"main": MirrorController("main")})
        async with TestClient(TestServer(app)) as client:
            assert (await client.post("/api/mirror", json={"screen": GOOD_SCREEN})).status == 403
            assert (await client.delete("/api/mirror", json={})).status == 403
            assert (await client.post("/api/mirror/end", json={})).status == 403
    asyncio.run(wrapper())
