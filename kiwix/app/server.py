"""Supervision of the kiwix-serve process.

kiwix-serve is started on loopback only and reached exclusively through the
manager's /kiwix proxy, so every request to it has already passed Home
Assistant's authentication. Two details matter:

* `--urlRootLocation` has to be the *full* public prefix, because kiwix-serve
  writes absolute links into the pages it serves. Under ingress that prefix
  is Home Assistant's own `/api/hassio_ingress/<token>` plus our `/kiwix`.
  The token is discovered at start-up but also re-checked against the
  `X-Ingress-Path` header of live requests, so a rotated token heals itself.
* `--monitorLibrary` makes kiwix-serve reload the library XML on its own, so
  adding or removing an archive never needs a restart.
"""

import collections
import socket
import subprocess
import threading
import time

import library
import settings

_lock = threading.RLock()
_process: subprocess.Popen | None = None
_root = ""
_log: collections.deque[str] = collections.deque(maxlen=25)
_last_error = ""
_monitor_started = False


def root_for(ingress_prefix: str | None = None) -> str:
    prefix = (ingress_prefix if ingress_prefix is not None else settings.INGRESS_ENTRY).rstrip("/")
    return f"{prefix}/kiwix"


def current_root() -> str:
    with _lock:
        return _root


def is_running() -> bool:
    with _lock:
        return _process is not None and _process.poll() is None


def port_ready(timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", settings.KIWIX_PORT), timeout=timeout):
            return True
    except OSError:
        return False


def wait_ready(timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if port_ready():
            return True
        if not is_running():
            return False
        time.sleep(0.2)
    return False


def _drain(process: subprocess.Popen) -> None:
    """Mirror kiwix-serve output into the add-on log, keeping a tail for the UI."""
    assert process.stdout is not None
    for line in process.stdout:
        line = line.rstrip()
        if line:
            _log.append(line)
            print(f"[kiwix-serve] {line}")


def start(ingress_prefix: str | None = None) -> tuple[bool, str]:
    """Start kiwix-serve (idempotent). Returns (started_ok, error_message)."""
    global _process, _root, _last_error

    status = settings.storage_status()
    if not status["ok"]:
        _last_error = status["message"]
        return False, status["message"]

    with _lock:
        if is_running():
            return True, ""

        library.rebuild_library_xml()
        _root = root_for(ingress_prefix)

        command = [
            "kiwix-serve",
            "--library", str(library.library_xml()),
            "--port", str(settings.KIWIX_PORT),
            "--address", "127.0.0.1",
            "--urlRootLocation", _root,
            "--monitorLibrary",
            "--skipInvalid",
        ]
        try:
            _process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            _last_error = f"Could not start kiwix-serve: {exc}"
            _process = None
            return False, _last_error

        threading.Thread(target=_drain, args=(_process,), daemon=True).start()
        print(f"[kiwix] kiwix-serve started on 127.0.0.1:{settings.KIWIX_PORT} at root {_root}")

    if not wait_ready():
        _last_error = "kiwix-serve did not start listening. Check the add-on log."
        return False, _last_error

    _last_error = ""
    return True, ""


def stop() -> None:
    global _process
    with _lock:
        process, _process = _process, None
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
    print("[kiwix] kiwix-serve stopped")


def restart(ingress_prefix: str | None = None) -> tuple[bool, str]:
    stop()
    return start(ingress_prefix)


def ensure_root(ingress_prefix: str) -> None:
    """Re-point kiwix-serve at a changed ingress prefix.

    Home Assistant's ingress token is stable in practice but not guaranteed
    to be; if a live request arrives under a different prefix than the one
    kiwix-serve is generating links for, every link on its pages would be
    broken until the add-on was restarted by hand. Restarting here instead
    costs one slow request and fixes it permanently.
    """
    wanted = root_for(ingress_prefix)
    with _lock:
        if not is_running() or wanted == _root:
            return
        print(f"[kiwix] ingress path changed ({_root} -> {wanted}); restarting kiwix-serve")
    restart(ingress_prefix)


def enabled() -> bool:
    return library.read_state()["server_enabled"]


def set_enabled(value: bool, ingress_prefix: str | None = None) -> tuple[bool, str]:
    state = library.read_state()
    state["server_enabled"] = value
    library.write_state(state)
    if value:
        return start(ingress_prefix)
    stop()
    return True, ""


def status() -> dict:
    running = is_running()
    return {
        "running": running,
        "enabled": enabled(),
        "ready": running and port_ready(),
        "root": current_root(),
        "served_count": len(library.read_state()["served"]),
        "error": _last_error,
        "log": list(_log)[-8:],
    }


def _monitor() -> None:
    """Restart kiwix-serve if it exits while it is meant to be running."""
    backoff = 5.0
    while True:
        time.sleep(5)
        try:
            if not enabled() or is_running():
                backoff = 5.0
                continue
            if not settings.storage_status()["ok"]:
                continue
            if not library.read_state()["served"]:
                # Nothing selected: there is nothing to serve, and that is a
                # normal idle state rather than a failure to retry.
                continue
            print("[kiwix] kiwix-serve is not running; restarting it")
            ok, _ = start()
            backoff = 5.0 if ok else min(backoff * 2, 120.0)
            time.sleep(0 if ok else backoff)
        except Exception as exc:  # noqa: BLE001 - the supervisor must not die
            print(f"[kiwix] supervisor error: {exc}")


def start_monitor() -> None:
    global _monitor_started
    with _lock:
        if _monitor_started:
            return
        _monitor_started = True
    threading.Thread(target=_monitor, name="kiwix-supervisor", daemon=True).start()
