from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import catalog
import downloads
import library
import main
import server
import settings

# main.app's lifespan starts the kiwix-serve supervisor thread; using the
# client bare (not as a context manager) keeps the suite free of it.
client = TestClient(main.app)

INGRESS = {"X-Ingress-Path": "/api/hassio_ingress/tok3n"}


def test_the_panel_renders():
    response = client.get("/")
    assert response.status_code == 200
    assert "Kiwix" in response.text
    assert "Browse &amp; download" in response.text


def test_state_reports_an_unconfigured_share_without_failing(monkeypatch):
    monkeypatch.setenv("ZIM_PATH", "")
    body = client.get("/api/state").json()

    assert body["storage"]["ok"] is False
    assert body["storage"]["state"] == "unset"
    assert body["library"] == []
    assert body["options"]["zim_path"] == ""


def test_state_lists_the_share_contents(zim_dir):
    (Path(zim_dir) / "wikipedia_en_mini.zim").write_bytes(b"zim")

    body = client.get("/api/state").json()

    assert body["storage"]["ok"] is True
    assert [item["filename"] for item in body["library"]] == ["wikipedia_en_mini.zim"]
    assert body["server"]["running"] is False


def test_catalog_errors_surface_as_a_readable_message(monkeypatch):
    def boom(**kwargs):
        raise catalog.CatalogError("Could not reach the library at https://library.kiwix.org")

    monkeypatch.setattr(catalog, "search", boom)

    response = client.get("/api/catalog?q=wikipedia")

    assert response.status_code == 502
    assert "Could not reach the library" in response.json()["error"]


def test_catalog_marks_entries_already_on_the_share(monkeypatch, zim_dir):
    (Path(zim_dir) / "wikipedia_en_mini.zim").write_bytes(b"zim")
    monkeypatch.setattr(
        catalog,
        "search",
        lambda **kwargs: {
            "entries": [
                {"filename": "wikipedia_en_mini.zim", "title": "have it"},
                {"filename": "wikipedia_en_maxi.zim", "title": "want it"},
            ],
            "total": 2,
            "start": 0,
            "count": 30,
        },
    )

    entries = client.get("/api/catalog?q=wikipedia").json()["entries"]

    assert entries[0]["downloaded"] is True
    assert entries[1]["downloaded"] is False


def test_starting_a_download_without_storage_is_a_400(monkeypatch):
    monkeypatch.setenv("ZIM_PATH", "GhostNAS/Kiwix")

    response = client.post(
        "/api/downloads",
        json={"url": "https://example.org/a.zim", "filename": "a.zim"},
    )

    assert response.status_code == 400
    assert "isn't mounted" in response.json()["error"]


def test_unknown_download_actions_are_rejected():
    assert client.post("/api/downloads/abc/detonate").status_code == 404
    assert client.post("/api/downloads/abc/cancel").status_code == 400


def test_serving_an_archive_updates_the_selection(zim_dir, monkeypatch):
    (Path(zim_dir) / "wikipedia_en_mini.zim").write_bytes(b"zim")
    monkeypatch.setattr(server, "enabled", lambda: False)

    response = client.post("/api/library/wikipedia_en_mini.zim/serve", json={"served": True})

    assert response.status_code == 200
    assert library.read_state()["served"] == ["wikipedia_en_mini.zim"]


def test_serving_an_unknown_archive_is_refused(zim_dir):
    response = client.post("/api/library/ghost.zim/serve", json={"served": True})
    assert response.status_code == 400


def test_deleting_an_archive_removes_it(zim_dir):
    (Path(zim_dir) / "wikipedia_en_mini.zim").write_bytes(b"zim")

    response = client.request("DELETE", "/api/library/wikipedia_en_mini.zim")

    assert response.status_code == 200
    assert not (Path(zim_dir) / "wikipedia_en_mini.zim").exists()


def test_the_reader_explains_itself_when_kiwix_serve_cannot_start(monkeypatch):
    monkeypatch.setattr(server, "is_running", lambda: False)
    monkeypatch.setattr(server, "start", lambda prefix=None: (False, "nothing selected to serve"))

    response = client.get("/kiwix/", headers=INGRESS)

    assert response.status_code == 503
    assert "nothing selected to serve" in response.text


def test_the_reader_refuses_the_direct_port_when_links_are_ingress_shaped(monkeypatch):
    monkeypatch.setattr(settings, "INGRESS_ENTRY", "/api/hassio_ingress/tok3n")

    response = client.get("/kiwix/")

    assert response.status_code == 421
    assert "sidebar" in response.text


def test_the_reader_is_proxied_under_the_public_ingress_prefix(monkeypatch):
    sent = {}

    class _Upstream:
        status_code = 200
        headers = httpx.Headers({"content-type": "text/html", "transfer-encoding": "chunked"})

        async def aiter_raw(self):
            yield b"<html>article</html>"

        async def aclose(self):
            sent["closed"] = True

    async def fake_send(request, stream=False):
        sent["url"] = str(request.url)
        sent["range"] = request.headers.get("range")
        return _Upstream()

    monkeypatch.setattr(server, "is_running", lambda: True)
    monkeypatch.setattr(server, "ensure_root", lambda prefix: sent.setdefault("root", prefix))
    monkeypatch.setattr(main._kiwix_client, "send", fake_send)

    response = client.get(
        "/kiwix/viewer#wikipedia_en_mini/A/Home",
        headers={**INGRESS, "Range": "bytes=0-99"},
    )

    assert response.status_code == 200
    assert response.text == "<html>article</html>"
    # kiwix-serve generates absolute links from its own root, so it has to be
    # asked for the full public path, prefix included.
    assert sent["url"].endswith("/api/hassio_ingress/tok3n/kiwix/viewer")
    assert sent["range"] == "bytes=0-99"
    assert sent["root"] == "/api/hassio_ingress/tok3n"
    # Hop-by-hop headers must not be copied through the proxy.
    assert "transfer-encoding" not in {key.lower() for key in response.headers}


def test_server_actions_are_validated(monkeypatch):
    monkeypatch.setattr(server, "set_enabled", lambda value, prefix=None: (True, ""))
    assert client.post("/api/server/start", headers=INGRESS).status_code == 200
    assert client.post("/api/server/levitate").status_code == 404
