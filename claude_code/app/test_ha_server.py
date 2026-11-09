"""Tests for the HA Server additions to the Claude Code app.

The upstream app is a Dockerfile and shell scripts with no Python, so these
tests drive the two added scripts through bash and check that the app's
config, translations and Dockerfile stay consistent with them.
"""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

APP = Path(__file__).resolve().parent.parent
BIN = APP / "rootfs" / "usr" / "local" / "bin"
IMPORT = BIN / "ha-server-import-data.sh"
REMOTE = BIN / "ha-server-remote-control.sh"
PRELOAD_SRC = APP / "rootfs" / "usr" / "src" / "ha-server" / "hostname_override.c"

needs_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")


def run(script, *args, env=None):
    full_env = dict(os.environ, **(env or {}))
    return subprocess.run(
        ["bash", str(script), *args],
        capture_output=True, text=True, env=full_env, timeout=30,
    )


# --- data import -----------------------------------------------------------

@pytest.fixture
def dirs(tmp_path):
    stage = tmp_path / "stage"
    data = tmp_path / "data"
    stage.mkdir()
    data.mkdir()
    env = {"HA_SERVER_IMPORT_DIR": str(stage), "HA_SERVER_DATA_DIR": str(data)}
    return stage, data, env


def test_import_copies_everything_but_options_and_removes_stage(dirs):
    stage, data, env = dirs
    (stage / "npm-global" / "codex-home").mkdir(parents=True)
    (stage / "npm-global" / "gh_pantry_token").write_text("secret")
    (stage / "npm-global" / ".seed-complete").write_text("")
    (stage / ".hidden").write_text("dot")
    (stage / "options.json").write_text('{"old": true}')
    (stage / ".stage-complete").write_text("")
    (data / "options.json").write_text('{"new": true}')

    res = run(IMPORT, env=env)

    assert res.returncode == 0, res.stdout + res.stderr
    assert (data / "npm-global" / "gh_pantry_token").read_text() == "secret"
    assert (data / "npm-global" / ".seed-complete").exists()
    assert (data / "npm-global" / "codex-home").is_dir()
    assert (data / ".hidden").read_text() == "dot"
    assert json.loads((data / "options.json").read_text()) == {"new": True}
    assert not (data / ".stage-complete").exists()
    assert (data / ".ha-server-imported").exists()
    assert not stage.exists()


def test_import_keeps_existing_entry_aside(dirs):
    stage, data, env = dirs
    (stage / "gradle").mkdir()
    (stage / "gradle" / "gradle.properties").write_text("old-app")
    (stage / ".stage-complete").write_text("")
    (data / "gradle").mkdir()
    (data / "gradle" / "gradle.properties").write_text("this-app")

    assert run(IMPORT, env=env).returncode == 0
    assert (data / "gradle" / "gradle.properties").read_text() == "old-app"
    assert (data / "gradle.pre-import" / "gradle.properties").read_text() == "this-app"


def test_import_waits_for_stage_complete(dirs):
    stage, data, env = dirs
    (stage / "npm-global").mkdir()

    res = run(IMPORT, env=env)

    assert res.returncode == 0
    assert "no .stage-complete" in res.stdout
    assert not (data / "npm-global").exists()
    assert stage.exists()


def test_import_runs_only_once(dirs):
    stage, data, env = dirs
    (data / ".ha-server-imported").write_text("2026-11-09T00:00:00Z")
    (stage / "npm-global").mkdir()
    (stage / ".stage-complete").write_text("")

    res = run(IMPORT, env=env)

    assert res.returncode == 0
    assert "already imported" in res.stdout
    assert not (data / "npm-global").exists()


def test_import_without_stage_is_silent(tmp_path):
    env = {"HA_SERVER_IMPORT_DIR": str(tmp_path / "missing"),
           "HA_SERVER_DATA_DIR": str(tmp_path / "data")}
    res = run(IMPORT, env=env)
    assert res.returncode == 0
    assert res.stdout == ""


# --- remote control arguments ------------------------------------------------

def rc_args(tmp_path, options):
    opts = tmp_path / "options.json"
    opts.write_text(json.dumps(options))
    res = run(REMOTE, "args", env={
        "HA_SERVER_OPTIONS": str(opts),
        "HA_SERVER_RC_LOG": str(tmp_path / "rc.log"),
    })
    assert res.returncode == 0, res.stderr
    return res.stdout.splitlines()


@needs_jq
def test_args_from_options(tmp_path):
    args = rc_args(tmp_path, {
        "remote_control_spawn": "worktree",
        "remote_control_capacity": 4,
        "remote_control_permission_mode": "acceptEdits",
        "remote_control_device_name": "HA Server",
    })
    assert args == [
        "remote-control", "--spawn", "worktree", "--capacity", "4",
        "--permission-mode", "acceptEdits",
        "--remote-control-session-name-prefix", "HA Server",
    ]


@needs_jq
def test_args_defaults_and_no_name(tmp_path):
    args = rc_args(tmp_path, {"remote_control_device_name": ""})
    assert args == ["remote-control", "--spawn", "same-dir", "--capacity", "8",
                    "--permission-mode", "default"]


@needs_jq
def test_bypass_permissions_is_never_passed(tmp_path):
    args = rc_args(tmp_path, {"remote_control_permission_mode": "bypassPermissions",
                              "remote_control_spawn": "nonsense",
                              "remote_control_capacity": "lots"})
    assert "bypassPermissions" not in args
    assert args[args.index("--permission-mode") + 1] == "default"
    assert args[args.index("--spawn") + 1] == "same-dir"
    assert args[args.index("--capacity") + 1] == "8"


@needs_jq
def test_start_does_nothing_when_disabled(tmp_path):
    opts = tmp_path / "options.json"
    opts.write_text(json.dumps({"remote_control_server": False}))
    res = run(REMOTE, "start", str(tmp_path), env={"HA_SERVER_OPTIONS": str(opts)})
    assert res.returncode == 0
    assert "disabled" in res.stdout


# --- consistency -------------------------------------------------------------

def test_options_schema_and_translations_agree():
    config = yaml.safe_load((APP / "config.yaml").read_text())
    en = yaml.safe_load((APP / "translations" / "en.yaml").read_text())
    assert set(config["options"]) == set(config["schema"])
    assert set(config["schema"]) <= set(en["configuration"])
    assert "bypassPermissions" not in config["schema"]["remote_control_permission_mode"]


def startup_script():
    text = (APP / "Dockerfile").read_text()
    cmd = text[text.index("CMD [") + 4:].replace("\\\n", "")
    return json.loads(cmd.strip())[2]


def test_startup_command_is_valid_json_and_bash():
    script = startup_script()
    res = subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


def test_startup_imports_before_seeding_and_starts_server_before_ttyd():
    script = startup_script()
    imp = script.index("ha-server-import-data.sh")
    seed = script.index("npm-global/.seed-complete")
    rc = script.index("ha-server-remote-control.sh start")
    ttyd = script.index("exec ttyd")
    assert imp < seed
    assert rc < ttyd


def test_dockerfile_ships_every_added_script():
    text = (APP / "Dockerfile").read_text()
    for script in (IMPORT, REMOTE):
        assert re.search(rf"COPY rootfs/usr/local/bin/{script.name} ", text)
        assert os.access(script, os.X_OK)


def test_startup_marks_the_background_update():
    script = startup_script()
    touch = script.index("touch /tmp/claude-update.running")
    fork = script.index("npm install -g @anthropic-ai/claude-code@latest")
    rm = script.index("rm -f /tmp/claude-update.running")
    rc = script.index("ha-server-remote-control.sh start")
    # The marker exists before the subshell is forked and before the server
    # starts, and the subshell removes it whichever way npm ends.
    assert touch < fork < rm < rc


# --- update wait -----------------------------------------------------------

@needs_jq
def test_wait_update_returns_at_once_without_marker(tmp_path):
    opts = tmp_path / "options.json"
    opts.write_text("{}")
    env = {"HA_SERVER_OPTIONS": str(opts),
           "HA_SERVER_UPDATE_MARKER": str(tmp_path / "missing"),
           "HA_SERVER_RC_LOG": str(tmp_path / "rc.log")}
    res = run(REMOTE, "wait-update", env=env)
    assert res.returncode == 0
    assert res.stdout == ""


@needs_jq
def test_wait_update_waits_for_marker_to_go(tmp_path):
    opts = tmp_path / "options.json"
    opts.write_text("{}")
    marker = tmp_path / "updating"
    marker.touch()
    env = dict(os.environ, HA_SERVER_OPTIONS=str(opts),
               HA_SERVER_UPDATE_MARKER=str(marker),
               HA_SERVER_RC_LOG=str(tmp_path / "rc.log"))
    proc = subprocess.Popen(["bash", str(REMOTE), "wait-update"], env=env,
                            stdout=subprocess.PIPE, text=True)
    with pytest.raises(subprocess.TimeoutExpired):
        proc.wait(timeout=3)
    marker.unlink()
    out, _ = proc.communicate(timeout=10)
    assert proc.returncode == 0
    assert "waiting for the Claude Code update" in out
    assert "anyway" not in out


# --- hostname override -----------------------------------------------------

def _cc():
    for name in (os.environ.get("CC"), "gcc", "cc", "clang"):
        if name and shutil.which(name.split()[0]):
            return name.split()
    return None


@pytest.fixture(scope="module")
def preload(tmp_path_factory):
    cc = _cc()
    if cc is None:
        pytest.skip("no C compiler")
    out = tmp_path_factory.mktemp("preload")
    lib = out / "hostname_override.so"
    subprocess.run([*cc, "-shared", "-fPIC", "-O2", "-Wall", "-Werror",
                    "-o", str(lib), str(PRELOAD_SRC)], check=True)
    other = out / "other.c"
    other.write_text("int ha_server_test_other;\n")
    other_lib = out / "libother.so"
    subprocess.run([*cc, "-shared", "-fPIC", "-o", str(other_lib), str(other)], check=True)
    return lib, other_lib


PROBE = """
import json, os, socket, subprocess, sys
child = subprocess.run([sys.executable, "-c",
    "import json, os, socket; print(json.dumps([socket.gethostname(), os.uname().nodename,"
    " os.environ.get('LD_PRELOAD'), os.environ.get('HA_SERVER_HOSTNAME')]))"],
    capture_output=True, text=True, check=True)
print(json.dumps({"self": [socket.gethostname(), os.uname().nodename,
                           os.environ.get("LD_PRELOAD"), os.environ.get("HA_SERVER_HOSTNAME")],
                  "child": json.loads(child.stdout)}))
"""


def _probe(preload_value, name="HA Test"):
    env = dict(os.environ, LD_PRELOAD=preload_value, HA_SERVER_HOSTNAME=name)
    res = subprocess.run(["python3", "-I", "-c", PROBE], env=env,
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])


def test_override_applies_to_the_process_but_not_its_children(preload):
    lib, _ = preload
    real = subprocess.run(["python3", "-c", "import socket; print(socket.gethostname())"],
                          capture_output=True, text=True, check=True).stdout.strip()
    out = _probe(str(lib))
    assert out["self"] == ["HA Test", "HA Test", None, None]
    assert out["child"] == [real, real, None, None]


def test_other_preload_entries_are_kept(preload):
    lib, other = preload
    out = _probe(f"{lib} {other}")
    assert out["self"][0] == "HA Test"
    assert out["self"][2] == str(other)
    assert out["child"][2] == str(other)


def test_empty_name_leaves_hostname_alone(preload):
    lib, _ = preload
    real = subprocess.run(["python3", "-c", "import socket; print(socket.gethostname())"],
                          capture_output=True, text=True, check=True).stdout.strip()
    out = _probe(str(lib), name="")
    assert out["self"][0] == real
    assert out["self"][2] is None
