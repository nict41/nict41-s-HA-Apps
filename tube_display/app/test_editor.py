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
