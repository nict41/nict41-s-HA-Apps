"""The standalone Docker packaging: platform wording, Basic auth and the
reverse-proxy base path. None of it may change the add-on's behaviour."""

import base64

from fastapi.testclient import TestClient

import main
import server
import settings

client = TestClient(main.app)


def _basic(username: str, password: str) -> dict:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _require_login(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_USERNAME", "friend")
    monkeypatch.setattr(settings, "AUTH_PASSWORD", "s3cret")


def test_no_credentials_configured_means_no_login(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_USERNAME", "")
    monkeypatch.setattr(settings, "AUTH_PASSWORD", "")
    assert client.get("/").status_code == 200


def test_configured_credentials_are_required_everywhere(monkeypatch):
    _require_login(monkeypatch)

    for path in ("/", "/read", "/api/state", "/kiwix/"):
        response = client.get(path)
        assert response.status_code == 401, path
        assert response.headers["WWW-Authenticate"].startswith("Basic ")


def test_wrong_or_malformed_credentials_are_refused(monkeypatch):
    _require_login(monkeypatch)

    assert client.get("/", headers=_basic("friend", "nope")).status_code == 401
    assert client.get("/", headers=_basic("someone", "s3cret")).status_code == 401
    assert client.get("/", headers={"Authorization": "Basic !!!"}).status_code == 401
    assert client.get("/", headers={"Authorization": "Bearer abc"}).status_code == 401


def test_the_right_credentials_open_the_app(monkeypatch):
    _require_login(monkeypatch)

    assert client.get("/", headers=_basic("friend", "s3cret")).status_code == 200
    assert client.get("/api/state", headers=_basic("friend", "s3cret")).status_code == 200


def test_the_health_check_needs_no_credentials(monkeypatch):
    _require_login(monkeypatch)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_the_base_path_becomes_the_reader_root(monkeypatch):
    monkeypatch.setattr(settings, "INGRESS_ENTRY", "")
    monkeypatch.setattr(settings, "BASE_PATH", "/kiwix-app")
    assert server.root_for() == "/kiwix-app/kiwix"


def test_ingress_still_wins_over_the_base_path(monkeypatch):
    monkeypatch.setattr(settings, "BASE_PATH", "/kiwix-app")
    assert server.root_for("/api/hassio_ingress/tok3n") == "/api/hassio_ingress/tok3n/kiwix"


def test_docker_wording_points_at_the_environment(monkeypatch):
    monkeypatch.setattr(settings, "IS_DOCKER", True)
    monkeypatch.setenv("ZIM_PATH", "")
    message = settings.storage_status()["message"]
    assert "ZIM_PATH" in message
    assert "add-on" not in message

    page = client.get("/").text
    assert "container" in page
    assert "add-on restart" not in page


def test_addon_wording_is_unchanged(monkeypatch):
    monkeypatch.setattr(settings, "IS_DOCKER", False)
    monkeypatch.setenv("ZIM_PATH", "")
    assert "add-on" in settings.storage_status()["message"]
    assert "add-on restart" in client.get("/").text
