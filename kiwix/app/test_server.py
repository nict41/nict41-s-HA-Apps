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


class _FakeProcess:
    def __init__(self, command, **kwargs):
        self.command = command
        self.stdout = iter(())
        self.stopped = False

    def poll(self):
        return 0 if self.stopped else None

    def terminate(self):
        self.stopped = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.stopped = True


def test_the_lan_reader_is_off_unless_a_port_is_set(monkeypatch):
    monkeypatch.setattr(settings, "LAN_PORT", 0)
    started = []
    monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **k: started.append(a))

    assert server.start_lan() == (True, "")
    assert started == []
    assert server.status()["lan"]["enabled"] is False


def test_the_lan_reader_listens_on_all_interfaces_at_the_url_root(monkeypatch, zim_dir):
    monkeypatch.setattr(settings, "LAN_PORT", 8080)
    monkeypatch.setattr(settings, "LAN_HOST_PORT", 8181)
    processes = []

    def fake_popen(command, **kwargs):
        processes.append(_FakeProcess(command))
        return processes[-1]

    monkeypatch.setattr(server.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(server, "port_ready", lambda timeout=0.5, port=None: True)

    try:
        ok, error = server.start()
        assert (ok, error) == (True, "")
        assert len(processes) == 2

        main_cmd, lan_cmd = processes[0].command, processes[1].command
        assert main_cmd[main_cmd.index("--address") + 1] == "127.0.0.1"
        assert "--address" not in lan_cmd
        assert lan_cmd[lan_cmd.index("--port") + 1] == "8080"
        assert "--urlRootLocation" not in lan_cmd
        assert lan_cmd[lan_cmd.index("--library") + 1] == main_cmd[main_cmd.index("--library") + 1]

        lan = server.status()["lan"]
        assert lan == {"enabled": True, "port": 8181, "running": True, "error": ""}

        server.set_enabled(False)
        assert all(p.stopped for p in processes)
        assert server.status()["lan"]["running"] is False
    finally:
        server.stop_all()


def test_a_lan_reader_failure_does_not_break_the_main_reader(monkeypatch, zim_dir):
    monkeypatch.setattr(settings, "LAN_PORT", 8080)

    def fake_popen(command, **kwargs):
        if str(settings.LAN_PORT) in command:
            raise OSError("address in use")
        return _FakeProcess(command)

    monkeypatch.setattr(server.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(server, "port_ready", lambda timeout=0.5, port=None: True)

    try:
        assert server.start() == (True, "")
        assert server.is_running() is True
        lan = server.status()["lan"]
        assert lan["running"] is False
        assert "address in use" in lan["error"]
    finally:
        server.stop_all()
