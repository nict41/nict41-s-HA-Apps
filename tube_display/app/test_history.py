"""SaveHistory (history.py) unit tests, plus the editor's /api/history endpoints.

The SaveHistory tests need no Pillow. The editor endpoint tests import editor
lazily inside each test and are marked requires_pil (editor imports render,
which imports PIL at module load); they are skipped where Pillow is missing.
Async tests follow test_api.py: asyncio.run.
"""

import asyncio
import os
import re
from datetime import datetime

import pytest

from conftest import requires_pil
from history import SaveHistory

ID_RE = re.compile(r"^screens-\d{8}-\d{6}-\d{6}$")

YAML_A = """\
rotation: [a]
screens:
  a:
    tubes:
      - {type: blank}
      - {type: blank}
      - {type: blank}
      - {type: blank}
      - {type: blank}
      - {type: blank}
"""

YAML_B = YAML_A + "# changed\n"


def _run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------------
# SaveHistory unit tests
# ----------------------------------------------------------------------

def test_snapshot_writes_file_and_returns_id(tmp_path):
    screens = tmp_path / "screens.yaml"
    screens.write_text(YAML_A, encoding="utf-8")
    h = SaveHistory(str(screens))
    id1 = h.snapshot(YAML_A)
    assert id1 is not None and ID_RE.match(id1)
    path = tmp_path / ".history" / (id1 + ".yaml")
    assert path.read_text(encoding="utf-8") == YAML_A
    # a different text gets a new snapshot
    id2 = h.snapshot(YAML_B)
    assert id2 is not None and id2 != id1
    assert len(h.list()) == 2


def test_snapshot_skips_identical_to_newest(tmp_path):
    screens = tmp_path / "screens.yaml"
    screens.write_text(YAML_A, encoding="utf-8")
    h = SaveHistory(str(screens))
    assert h.snapshot(YAML_A) is not None
    assert h.snapshot(YAML_A) is None          # same as newest: skipped
    assert h.snapshot(YAML_B) is not None
    assert h.snapshot(YAML_A) is not None      # different from newest now
    assert len(h.list()) == 3


def test_list_newest_first_with_fields(tmp_path):
    screens = tmp_path / "screens.yaml"
    screens.write_text(YAML_A, encoding="utf-8")
    times = iter([datetime(2026, 11, 12, 10, 0, 0, 1),
                  datetime(2026, 11, 12, 10, 0, 0, 2),
                  datetime(2026, 11, 12, 10, 0, 0, 3)])
    h = SaveHistory(str(screens), now=lambda: next(times))
    h.snapshot("one")
    h.snapshot("two!!")
    h.snapshot("three!!!")
    items = h.list()
    assert [i["id"] for i in items] == sorted((i["id"] for i in items), reverse=True)
    assert items[0]["size"] == len("three!!!")
    assert items[0]["saved_at"]  # ISO 8601 local time
    datetime.fromisoformat(items[0]["saved_at"])
    assert all(set(i) == {"id", "saved_at", "size"} for i in items)


def test_read_and_key_errors(tmp_path):
    screens = tmp_path / "screens.yaml"
    screens.write_text(YAML_A, encoding="utf-8")
    h = SaveHistory(str(screens))
    id1 = h.snapshot(YAML_A)
    assert h.read(id1) == YAML_A
    with pytest.raises(KeyError):
        h.read("screens-20261112-100000-000000")   # right shape, doesn't exist
    with pytest.raises(KeyError):
        h.read("../screens.yaml")
    with pytest.raises(KeyError):
        h.read("screens-2026-11-12")                # bad shape
    with pytest.raises(KeyError):
        h.read("")
    with pytest.raises(KeyError):
        h.read(None)


def test_prune_keeps_newest_keep(tmp_path):
    screens = tmp_path / "screens.yaml"
    screens.write_text(YAML_A, encoding="utf-8")
    h = SaveHistory(str(screens), keep=3)
    ids = [h.snapshot(f"text {i}") for i in range(6)]
    assert all(ids)
    items = h.list()
    assert len(items) == 3
    assert [i["id"] for i in items] == sorted(ids, reverse=True)[:3]
    # the pruned files are really gone
    folder = tmp_path / ".history"
    assert sorted(os.listdir(folder)) == sorted(i["id"] + ".yaml" for i in items)


def test_empty_history(tmp_path):
    screens = tmp_path / "screens.yaml"
    screens.write_text(YAML_A, encoding="utf-8")
    h = SaveHistory(str(screens))
    assert h.list() == []


# ----------------------------------------------------------------------
# editor endpoints (need Pillow: editor imports render)
# ----------------------------------------------------------------------

def _make_client(tmp_path, text=YAML_A):
    """Same shape as test_editor.py's _make_client; editor imported lazily."""
    import editor as editor_mod

    class StubHa:
        async def render_template(self, template):
            return "42"

        async def _request(self, method, path, **kwargs):
            return []

    class StubController:
        def __init__(self):
            import render as render_mod

            async def renderer(t):
                return "42"
            self._ctx = render_mod.RenderContext(renderer, session=None, ha=None)

    screens_file = tmp_path / "screens.yaml"
    screens_file.write_text(text, encoding="utf-8")
    app = editor_mod.create_app(StubController(), StubHa(), str(screens_file),
                                test_allow_local=True)
    from aiohttp.test_utils import TestClient, TestServer
    return TestClient(TestServer(app)), str(screens_file)


def _with_client(tmp_path, fn, text=YAML_A):
    async def wrapper():
        client, screens_file = _make_client(tmp_path, text)
        async with client:
            await fn(client, screens_file)
    return asyncio.run(wrapper())


@requires_pil
def test_history_endpoints_save_list_get_restore(tmp_path):
    async def check(client, screens_file):
        # nothing saved yet
        resp = await client.get("/api/history")
        assert resp.status == 200
        assert (await resp.json()) == {"items": []}

        cfg = await (await client.get("/api/config")).json()
        # save a new version -> the old text lands in the history
        resp = await client.post("/api/save_yaml", json={"yaml": YAML_B, "mtime": cfg["mtime"]})
        assert resp.status == 200, await resp.text()
        new_mtime = (await resp.json())["mtime"]
        assert open(screens_file, encoding="utf-8").read() == YAML_B

        resp = await client.get("/api/history")
        items = (await resp.json())["items"]
        assert len(items) == 1
        assert ID_RE.match(items[0]["id"])
        assert items[0]["size"] == len(YAML_A)
        assert items[0]["saved_at"]

        resp = await client.get(f"/api/history/{items[0]['id']}")
        assert resp.status == 200
        body = await resp.json()
        assert body == {"id": items[0]["id"], "yaml": YAML_A}

        # restore it: the file goes back to YAML_A and the restore itself is
        # snapshotted (so it can be undone)
        resp = await client.post(f"/api/history/{items[0]['id']}/restore",
                                 json={"mtime": new_mtime})
        assert resp.status == 200, await resp.text()
        body = await resp.json()
        assert body["ok"] is True
        assert body["mtime"] == os.path.getmtime(screens_file)
        assert open(screens_file, encoding="utf-8").read() == YAML_A
        items2 = (await (await client.get("/api/history")).json())["items"]
        assert len(items2) == 2
        assert (await (await client.get(f"/api/history/{items2[0]['id']}")).json())["yaml"] == YAML_B
    _with_client(tmp_path, check)


@requires_pil
def test_history_restore_conflict_and_unknown_id(tmp_path):
    async def check(client, screens_file):
        cfg = await (await client.get("/api/config")).json()
        await client.post("/api/save_yaml", json={"yaml": YAML_B, "mtime": cfg["mtime"]})
        items = (await (await client.get("/api/history")).json())["items"]
        hid = items[0]["id"]
        # stale mtime -> 409, same as api_save
        resp = await client.post(f"/api/history/{hid}/restore", json={"mtime": 12345.0})
        assert resp.status == 409
        assert (await resp.json())["error"] == "changed on disk"
        # unknown id -> 404
        resp = await client.get("/api/history/screens-20261112-100000-000000")
        assert resp.status == 404
        resp = await client.post("/api/history/screens-20261112-100000-000000/restore",
                                 json={"mtime": os.path.getmtime(screens_file)})
        assert resp.status == 404
        # bad id shape -> 404 as well
        resp = await client.get("/api/history/..%2F..%2Fscreens")
        assert resp.status == 404
        # missing mtime -> 400
        resp = await client.post(f"/api/history/{hid}/restore", json={})
        assert resp.status == 400
    _with_client(tmp_path, check)


@requires_pil
def test_history_restore_rejects_invalid_yaml(tmp_path):
    async def check(client, screens_file):
        # plant an invalid snapshot directly in the history folder
        hist = SaveHistory(screens_file)
        bad_id = hist.snapshot("rotation: [a]\nscreens: {a: {tubes: [{type: nope}]}}\n")
        assert bad_id is not None
        resp = await client.post(f"/api/history/{bad_id}/restore",
                                 json={"mtime": os.path.getmtime(screens_file)})
        assert resp.status == 400
        body = await resp.json()
        assert body["error"] == "invalid screens"
        assert body["errors"]
        # the file was not touched
        assert open(screens_file, encoding="utf-8").read() == YAML_A
    _with_client(tmp_path, check)


@requires_pil
def test_history_forbidden_without_ingress(tmp_path):
    async def check(client, screens_file):
        # built without test_allow_local: the test client's peer is refused
        pass
    # build the app the way the 403 test in test_editor.py does
    async def wrapper():
        import editor as editor_mod
        screens_file = tmp_path / "screens.yaml"
        screens_file.write_text(YAML_A, encoding="utf-8")

        class StubHa:
            async def render_template(self, t):
                return "42"

            async def _request(self, *a, **k):
                return []

        class StubController:
            def __init__(self):
                import render as render_mod

                async def renderer(t):
                    return "42"
                self._ctx = render_mod.RenderContext(renderer, session=None, ha=None)

        app = editor_mod.create_app(StubController(), StubHa(), str(screens_file),
                                    test_allow_local=False)
        from aiohttp.test_utils import TestClient, TestServer
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/history")
            assert resp.status == 403
            resp = await client.get("/api/history/screens-20261112-100000-000000")
            assert resp.status == 403
            resp = await client.post("/api/history/screens-20261112-100000-000000/restore",
                                     json={"mtime": 1.0})
            assert resp.status == 403
    _run(wrapper())
