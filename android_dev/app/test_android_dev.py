import asyncio
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest
from starlette.testclient import TestClient

import adb
import jobs
import keystore
import paths
import setup
import server

TOKEN = "test-token-that-is-at-least-32-characters"
HOST = "beb500c8-android-dev:8200"


def test_http_auth_health_and_mcp_protocol(monkeypatch):
    monkeypatch.setattr(server, "TOKEN_VALID", True)
    monkeypatch.setattr(server, "TOKEN", TOKEN)
    with TestClient(server.app, base_url="http://" + HOST) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.post("/mcp", json={}).status_code == 401
        assert client.post("/mcp", headers={"Authorization": "Bearer wrong"}, json={}).status_code == 401
        headers = {"Authorization": f"Bearer {TOKEN}"}
        initialized = client.post("/mcp", headers=headers, json={"jsonrpc":"2.0", "id":1,
            "method":"initialize", "params":{"protocolVersion":"2025-06-18", "capabilities":{},
            "clientInfo":{"name":"pytest","version":"1"}}})
        assert initialized.status_code == 200, initialized.text
        session = initialized.headers["mcp-session-id"]
        headers["Mcp-Session-Id"] = session
        listing = client.post("/mcp", headers=headers, json={"jsonrpc":"2.0", "id":2, "method":"tools/list", "params":{}})
        assert listing.status_code == 200 and "sdk_setup" in listing.text
        called = client.post("/mcp", headers=headers, json={"jsonrpc":"2.0", "id":3, "method":"tools/call",
            "params":{"name":"list_projects", "arguments":{}}})
        assert called.status_code == 200
        result = called.json()["result"]
        assert not result.get("isError") and json.loads(result["content"][0]["text"])["projects"] == []
        monkeypatch.setattr(server.adb, "screenshot", lambda device: {"path": "/x.png", "data": "aGk="})
        shot = client.post("/mcp", headers=headers, json={"jsonrpc":"2.0", "id":4, "method":"tools/call",
            "params":{"name":"screenshot", "arguments":{}}}).json()["result"]["content"]
        assert [item["type"] for item in shot] == ["text", "image"] and shot[1]["data"] == "aGk="
        failed = client.post("/mcp", headers=headers, json={"jsonrpc":"2.0", "id":5, "method":"tools/call",
            "params":{"name":"gradle_run", "arguments":{"project":"../x", "tasks":["a"]}}}).json()["result"]
        assert failed["isError"] and "Invalid project name" in failed["content"][0]["text"]
        foreign = client.post("/mcp", headers={"Authorization": f"Bearer {TOKEN}", "Host": "attacker.invalid:8200"}, json={})
        assert foreign.status_code == 421


def test_invalid_config_token_returns_503(monkeypatch):
    monkeypatch.setattr(server, "TOKEN_VALID", False)
    client = TestClient(server.app, base_url="http://" + HOST)
    assert client.post("/mcp", json={}).status_code == 503
    client.close()


def test_hostname_security_allows_supervisor_and_rejects_other_host():
    from mcp.server.transport_security import TransportSecuritySettings, TransportSecurityMiddleware
    settings = TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=["beb500c8-android-dev:8200"])
    middleware = TransportSecurityMiddleware(settings)
    assert middleware._validate_host(HOST)
    assert not middleware._validate_host("attacker.invalid:8200")


def test_token_guards_every_path_and_survives_non_ascii(monkeypatch):
    monkeypatch.setattr(server, "TOKEN_VALID", True)
    monkeypatch.setattr(server, "TOKEN", TOKEN)
    client = TestClient(server.app, base_url="http://" + HOST)
    assert client.get("/anything-else").status_code == 401
    assert client.post("/mcp", headers={"Authorization": "Bearer caf\u00e9".encode("latin-1")}, json={}).status_code == 401
    client.close()


def test_token_is_not_inherited_by_builds():
    assert "MCP_TOKEN" not in os.environ


def test_setup_rerun_with_tools_present_needs_no_network(tmp_path, monkeypatch):
    sdk = tmp_path / "sdk"; (sdk / "platform-tools").mkdir(parents=True); (sdk / "platform-tools" / "adb").touch()
    gradle = tmp_path / "gradle"; gradle.touch()
    monkeypatch.setattr(setup, "ANDROID_HOME", sdk)
    monkeypatch.setattr(setup, "GRADLE", gradle)
    monkeypatch.setattr(setup, "LICENSE_ACCEPTED", True)
    monkeypatch.setattr(setup, "fetch", lambda *a, **k: pytest.fail("download called"))
    assert setup.sdk_setup()["setup"] == "ready"


def test_install_zip_keeps_executable_bit(tmp_path, monkeypatch):
    archive = tmp_path / "good.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        info = zipfile.ZipInfo("gradle-9.8.2/bin/gradle"); info.external_attr = 0o100755 << 16
        zf.writestr(info, "#!/bin/sh\n")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    monkeypatch.setattr(setup.urllib.request, "urlopen", lambda *a, **k: archive.open("rb"))
    target = tmp_path / "tools" / "gradle"
    setup._install_zip("https://example.invalid", target, digest=digest, strip_dir="gradle-9.8.2")
    assert os.access(target / "bin" / "gradle", os.X_OK)


DAEMON = "import os,subprocess,sys; subprocess.Popen(['sleep','5']); print('done', flush=True)"


def test_process_run_returns_when_a_daemon_holds_the_pipe():
    import process
    started = time.monotonic()
    result = process.run([sys.executable, "-c", DAEMON], timeout=4)
    assert result.returncode == 0 and "done" in result.stdout and time.monotonic() - started < 3


def test_job_finishes_when_a_daemon_holds_the_pipe(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "JOBS", tmp_path)
    project = tmp_path / "p"; project.mkdir()
    monkeypatch.setattr(jobs, "project_path", lambda _name: project)
    monkeypatch.setattr(jobs, "gradle_properties", lambda: None)
    jobs.CURRENT = None
    job = jobs.start_job("p", [sys.executable, "-c", DAEMON], timeout=4)
    for _ in range(40):
        if jobs.status(job["job_id"])["state"] == "succeeded": break
        time.sleep(.05)
    info = jobs.status(job["job_id"])
    assert info["state"] == "succeeded" and "done" in info["log"]


def test_license_gate_never_downloads(monkeypatch):
    monkeypatch.setattr(setup, "LICENSE_ACCEPTED", False)
    monkeypatch.setattr(setup, "fetch", lambda *a, **k: pytest.fail("download called"))
    assert setup.sdk_setup()["setup"] == "waiting_for_license"


def test_checksum_mismatch_keeps_install_absent(tmp_path, monkeypatch):
    target = tmp_path / "install"
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zf: zf.writestr("cmdline-tools/bin/sdkmanager", "bad")
    monkeypatch.setattr(setup.urllib.request, "urlopen", lambda *a, **k: archive.open("rb"))
    with pytest.raises(ValueError): setup._install_zip("https://example.invalid", target, digest="wrong")
    assert not target.exists()


def test_project_traversal_and_symlink_escape(tmp_path, monkeypatch):
    root = tmp_path / "projects"; root.mkdir()
    monkeypatch.setattr(paths, "PROJECTS", root)
    with pytest.raises(ValueError): paths.project_path("../escape")
    outside = tmp_path / "outside"; outside.mkdir()
    (root / "link").symlink_to(outside)
    with pytest.raises(ValueError): paths.project_path("link")
    outside_apk = outside / "app.apk"; outside_apk.write_bytes(b"fake apk")
    with pytest.raises(ValueError): paths.project_file(str(outside_apk))
    (root / "escaped.apk").symlink_to(outside_apk)
    with pytest.raises(ValueError): paths.project_file(str(root / "escaped.apk"))


def test_pio_project_path_checks_roots_symlinks_and_platformio_ini(tmp_path, monkeypatch):
    root = tmp_path / "esp"; root.mkdir()
    (root / "platformio.ini").write_text("[env:ipstube]\n")
    project = root / "tube"; project.mkdir(); (project / "platformio.ini").write_text("[env:ipstube]\n")
    outside = tmp_path / "outside"; outside.mkdir(); (outside / "platformio.ini").write_text("")
    empty = root / "empty"; empty.mkdir()
    monkeypatch.setattr(paths, "PIO_ROOTS", [root])
    assert paths.pio_project_path(str(project)) == project.resolve()
    assert paths.pio_project_path(str(root)) == root.resolve()
    with pytest.raises(ValueError): paths.pio_project_path(str(outside))
    with pytest.raises(ValueError): paths.pio_project_path(str(root / ".." / "outside"))
    (root / "link").symlink_to(outside)
    with pytest.raises(ValueError): paths.pio_project_path(str(root / "link"))
    with pytest.raises(ValueError): paths.pio_project_path(str(empty))
    with pytest.raises(ValueError): paths.pio_project_path(str(root / "missing"))


def test_pio_run_validates_names_and_builds_command(tmp_path, monkeypatch):
    root = tmp_path / "esp"; project = root / "tube"; project.mkdir(parents=True)
    (project / "platformio.ini").write_text("[env:ipstube]\n")
    monkeypatch.setattr(paths, "PIO_ROOTS", [root])
    captured = {}
    def fake_start(project_name, command, env=None, timeout=None, cwd=None):
        captured.update(project=project_name, command=command, env=env, timeout=timeout, cwd=cwd)
        return {"job_id": "abc123def456", "state": "queued"}
    monkeypatch.setattr(jobs, "start_job", fake_start)
    for environment, targets in (("bad env", []), ("ipstube", ["bad target"]), ("ipstube", ["../bad"])):
        with pytest.raises(server.ToolError, match="Invalid PlatformIO"):
            asyncio.run(server.pio_run(str(project), environment, targets))
    assert not captured
    result = json.loads(asyncio.run(server.pio_run(str(project), "ipstube", ["buildfs", "upload"]))[0].text)
    assert result == {"job_id": "abc123def456", "state": "queued"}
    assert captured["command"] == ["nice", "-n", "10", "pio", "run", "-d", str(project.resolve()),
                                   "-e", "ipstube", "-t", "buildfs", "-t", "upload"]
    assert captured["env"] == {"PLATFORMIO_CORE_DIR": str(server.PIO_CORE), "CI": "1",
                               "PLATFORMIO_SETTING_ENABLE_TELEMETRY": "no"}
    assert captured["timeout"] == server.PIO_JOB_TIMEOUT
    assert captured["cwd"] == project.resolve()


def test_pio_artifacts_lists_build_and_release_files_newest_first(tmp_path, monkeypatch):
    root = tmp_path / "esp"; project = root / "tube"
    build = project / ".pio" / "build" / "ipstube"; build.mkdir(parents=True)
    releases = project / "releases"; releases.mkdir()
    (project / "platformio.ini").write_text("[env:ipstube]\n")
    for name in ("firmware.bin", "firmware.elf", "littlefs.bin", "partitions.bin", "bootloader.bin", "notes.txt"):
        (build / name).write_bytes(name.encode())
    (releases / "tube-1.bin").write_bytes(b"release")
    (releases / "ignore.elf").write_bytes(b"ignore")
    candidates = [path for path in build.iterdir() if path.is_file()] + [path for path in releases.iterdir() if path.is_file()]
    for path in candidates: os.utime(path, (1000, 1000))
    for index, path in enumerate((build / "firmware.bin", build / "firmware.elf", releases / "tube-1.bin")):
        os.utime(path, (1001 + index, 1001 + index))
    monkeypatch.setattr(paths, "PIO_ROOTS", [root])
    listed = json.loads(asyncio.run(server.pio_artifacts(str(project), "ipstube"))[0].text)["artifacts"]
    assert [Path(item["path"]).name for item in listed][:2] == ["tube-1.bin", "firmware.elf"]
    assert {Path(item["path"]).name for item in listed} == {
        "firmware.bin", "firmware.elf", "littlefs.bin", "partitions.bin", "bootloader.bin", "tube-1.bin"}
    with pytest.raises(server.ToolError, match="Invalid PlatformIO environment"):
        asyncio.run(server.pio_artifacts(str(project), "../ipstube"))


def test_start_job_with_cwd_is_still_single_flight(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "DATA", tmp_path)
    monkeypatch.setattr(jobs, "JOBS", tmp_path / "jobs"); jobs.JOBS.mkdir()
    cwd = tmp_path / "esp"; cwd.mkdir()
    monkeypatch.setattr(jobs, "project_path", lambda _name: pytest.fail("project_path called with cwd"))
    monkeypatch.setattr(jobs, "gradle_properties", lambda: None)
    jobs.CURRENT = None
    first = jobs.start_job("esp", [sys.executable, "-c", "import time; time.sleep(10)"], timeout=5, cwd=cwd)
    assert first["job_id"]
    assert "already running" in jobs.start_job("esp", ["/bin/true"], cwd=cwd)["error"]
    jobs.cancel(first["job_id"])
    for _ in range(60):
        if jobs.status(first["job_id"])["state"] == "cancelled": break
        time.sleep(.05)
    assert jobs.status(first["job_id"])["state"] == "cancelled"


def test_job_single_flight_cancel_timeout_and_redaction(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "DATA", tmp_path)
    monkeypatch.setattr(jobs, "JOBS", tmp_path / "jobs"); jobs.JOBS.mkdir()
    project = tmp_path / "p"; project.mkdir()
    monkeypatch.setattr(jobs, "project_path", lambda _name: project)
    jobs.CURRENT = None
    monkeypatch.setattr(jobs, "gradle_properties", lambda: None)
    first = jobs.start_job("p", [sys.executable, "-c", "import time; time.sleep(10)"], timeout=5)
    assert first["job_id"]
    assert "already running" in jobs.start_job("p", ["/bin/true"])["error"]
    time.sleep(.15)
    jobs.cancel(first["job_id"])
    for _ in range(40):
        if jobs.status(first["job_id"])["state"] == "cancelled": break
        time.sleep(.05)
    assert jobs.status(first["job_id"])["state"] == "cancelled"
    secret_env = {"ANDROID_SIGNING_STORE_PASSWORD":"secret-password"}
    log = jobs._redact(b"output secret-password", secret_env)
    assert b"secret-password" not in log and b"[REDACTED]" in log
    timed = jobs.start_job("p", [sys.executable, "-c", "import time; time.sleep(2)"], timeout=.2)
    for _ in range(40):
        if jobs.status(timed["job_id"])["state"] == "timed_out": break
        time.sleep(.05)
    assert jobs.status(timed["job_id"])["state"] == "timed_out"
    secret = "secret-that-crosses-a-write-boundary"
    code = "import os,sys,time; s=os.environ['ANDROID_SIGNING_STORE_PASSWORD']; sys.stdout.write(s[:12]); sys.stdout.flush(); time.sleep(.05); sys.stdout.write(s[12:])"
    logged = jobs.start_job("p", [sys.executable, "-c", code], {"ANDROID_SIGNING_STORE_PASSWORD": secret}, timeout=2)
    for _ in range(40):
        info = jobs.status(logged["job_id"])
        if info["state"] != "running" and info["state"] != "queued": break
        time.sleep(.05)
    info = jobs.status(logged["job_id"])
    assert info["state"] == "succeeded", (info.get("error"), info.get("log"), info)
    assert secret not in info["log"] and "[REDACTED]" in info["log"]


def test_adb_false_success_and_device_selection(monkeypatch):
    monkeypatch.setattr(adb.process, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "Failure [install failed]", ""))
    with pytest.raises(RuntimeError): adb.run(["install", "x.apk"])
    monkeypatch.setattr(adb, "devices", lambda: ["one device product:p", "two device product:p"])
    with pytest.raises(ValueError, match="exactly one"): adb.select()
    assert adb.select("one") == "one"


def test_devices_parses_real_adb_output(monkeypatch):
    output = ("* daemon not running; starting now at tcp:5037\n* daemon started successfully\n"
              "List of devices attached\n"
              "192.168.8.20:41233     device product:oriole model:Pixel_6 transport_id:1\n"
              "192.168.8.21:5555      offline transport_id:2\n"
              "adb-1A2B._adb-tls-connect._tcp device product:p model:m transport_id:3\n\n")
    monkeypatch.setattr(adb.process, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 0, output, ""))
    assert [row.split()[0] for row in adb.devices()] == ["192.168.8.20:41233", "adb-1A2B._adb-tls-connect._tcp"]


@pytest.mark.parametrize("output", ["cannot connect to 192.168.8.20:5555: Connection refused (111)",
                                    "** No activities found to run, monkey aborted."])
def test_exit_zero_failures_are_errors(monkeypatch, output):
    monkeypatch.setattr(adb.process, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 0, output, ""))
    with pytest.raises(RuntimeError): adb.run(["connect", "x:1"])


def test_list_artifacts_is_not_starved_by_report_assets(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "project_path", lambda _name: tmp_path)
    outputs = tmp_path / "app/build/outputs/apk/debug"; outputs.mkdir(parents=True)
    (outputs / "app-debug.apk").touch()
    reports = tmp_path / "app/build/reports/tests"; reports.mkdir(parents=True)
    (reports / "index.html").touch()
    for number in range(600): (reports / f"asset{number}.css").touch()
    listed = json.loads(asyncio.run(server.list_artifacts("p"))[0].text)
    assert sorted(Path(item["path"]).name for item in listed["artifacts"]) == ["app-debug.apk", "index.html"]


def test_input_text_escape():
    escaped = adb.input_escape("Hi there;$(touch /tmp/x)\n!")
    assert "%s" in escaped and "\\;" in escaped and "\\$" in escaped and "\\(" in escaped


def test_input_text_passes_escaped_value_as_argv(monkeypatch):
    captured = {}
    monkeypatch.setattr(adb, "device_run", lambda device, args, *rest: captured.update(args=args) or "OK")
    asyncio.run(server.input_text("hello world; $(id)"))
    assert captured["args"] == ["shell", "input", "text", "hello%sworld\\;%s\\$\\(id\\)"]


def test_keystore_no_overwrite_and_password_not_returned_or_argv(tmp_path, monkeypatch):
    monkeypatch.setattr(keystore, "ROOT", tmp_path)
    captured = {}
    def fake_run(argv, **kwargs):
        captured.update(argv=argv, env=kwargs["env"])
        (tmp_path / "demo.jks").write_text("fake")
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(keystore.process, "run", fake_run)
    monkeypatch.setattr(keystore, "fingerprint", lambda _path: "AA:BB")
    result = keystore.create("demo")
    assert "password" not in json.dumps(result).lower()
    assert captured["env"]["STORE_PASSWORD"] not in captured["argv"]
    assert "-keypass:env" not in captured["argv"]  # PKCS12 would ignore it
    stored = json.loads((tmp_path / "demo.json").read_text())
    assert stored["key_password"] == stored["store_password"]
    assert (tmp_path / "demo.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="overwrite"): keystore.create("demo")
