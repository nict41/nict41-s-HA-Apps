import library
import server
import settings


def test_the_serve_root_carries_the_ingress_prefix():
    assert server.root_for("/api/hassio_ingress/tok3n") == "/api/hassio_ingress/tok3n/kiwix"
    assert server.root_for("/api/hassio_ingress/tok3n/") == "/api/hassio_ingress/tok3n/kiwix"


def test_the_serve_root_falls_back_to_a_bare_path_off_ingress(monkeypatch):
    monkeypatch.setattr(settings, "INGRESS_ENTRY", "")
    assert server.root_for() == "/kiwix"


def test_starting_without_storage_reports_the_storage_problem(monkeypatch):
    monkeypatch.setenv("ZIM_PATH", "GhostNAS/Kiwix")

    started, error = server.start()

    assert started is False
    assert "isn't mounted" in error


def test_a_missing_kiwix_serve_binary_is_an_error_not_a_crash(monkeypatch, zim_dir):
    def no_binary(*args, **kwargs):
        raise FileNotFoundError("kiwix-serve")

    monkeypatch.setattr(server.subprocess, "Popen", no_binary)

    started, error = server.start()

    assert started is False
    assert "Could not start kiwix-serve" in error


def test_enabling_and_disabling_persists_across_restarts(monkeypatch, zim_dir):
    monkeypatch.setattr(server, "start", lambda prefix=None: (True, ""))

    server.set_enabled(False)
    assert library.read_state()["server_enabled"] is False
    assert server.enabled() is False

    server.set_enabled(True)
    assert server.enabled() is True


def test_ensure_root_does_nothing_while_the_server_is_down(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "restart", lambda prefix=None: calls.append(prefix))

    server.ensure_root("/api/hassio_ingress/other")

    assert calls == []


def test_status_is_renderable_with_no_server_running():
    status = server.status()
    assert status["running"] is False
    assert set(status) >= {"running", "enabled", "ready", "root", "served_count", "error", "log"}
