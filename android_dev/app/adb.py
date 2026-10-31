import base64
import re
import subprocess
import time
from io import BytesIO
from PIL import Image
from config import SCREENSHOTS
import process

HOST_PORT = re.compile(r"^(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:]+\]):[0-9]{1,5}$")
PKG = re.compile(r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+$")
# adb exits 0 for all of these.
FAILURE_MARKERS = ("failure", "failed", "error:", "cannot connect", "unable to connect", "monkey aborted")


def run(args, timeout=30, check=True):
    try:
        p = process.run(["adb", *map(str, args)], timeout=timeout, max_bytes=1024 * 1024)
    except FileNotFoundError as exc:
        raise RuntimeError("adb is unavailable until Android SDK setup installs platform-tools; enable the licence option and run sdk_setup") from exc
    out = p.stdout.strip()
    lowered = out.lower()
    false_success = any(marker in lowered for marker in FAILURE_MARKERS)
    if check and (p.returncode or false_success):
        raise RuntimeError(f"adb exited {p.returncode}: {out[-4000:]}")
    return out


def devices():
    try: out = run(["devices", "-l"], check=False)
    except RuntimeError: return []
    # `-l` pads columns with spaces, and daemon startup notices can precede the header.
    return [line for line in out.splitlines() if line.split()[1:2] == ["device"]]


def select(device=None):
    found = devices()
    if device:
        if not any(row.split()[0] == device for row in found): raise ValueError(f"Device {device!r} is not connected. Connected: {found}")
        return device
    if len(found) != 1: raise ValueError(f"Expected exactly one connected device; found {len(found)}: {found}")
    return found[0].split()[0]


def target_args(device=None): return ["-s", select(device)]


def device_run(device, args, timeout=30, check=True):
    return run([*target_args(device), *args], timeout=timeout, check=check)


def device_subprocess(device, args, timeout=30, **kwargs):
    try: return process.run(["adb", *target_args(device), *args], timeout=timeout, **kwargs)
    except FileNotFoundError as exc:
        raise RuntimeError("adb is unavailable until Android SDK setup installs platform-tools; enable the licence option and run sdk_setup") from exc


def pair(host_port, code):
    validate_host_port(host_port)
    return run(["pair", host_port, code], timeout=60)


def connect(host_port):
    validate_host_port(host_port)
    return run(["connect", host_port], timeout=60)


def validate_host_port(value):
    if not HOST_PORT.fullmatch(value): raise ValueError("Expected host:port")
    port = int(value.rsplit(":", 1)[1])
    if not 1 <= port <= 65535: raise ValueError("Port must be between 1 and 65535")


def input_escape(text):
    # adb's remote shell tokenizes its command; percent-encode whitespace and shell metacharacters.
    return re.sub(r"([^A-Za-z0-9_.,@%+=:-])", lambda m: "%s" if m.group(1).isspace() else "\\" + m.group(1), text)


def screenshot(device=None):
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    filename = f"screen-{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1000000:06d}.png"
    path = SCREENSHOTS / filename
    try:
        with path.open("wb") as output:
            data = subprocess.run(["adb", *target_args(device), "exec-out", "screencap", "-p"], stdout=output, stderr=subprocess.PIPE, timeout=30)
    except FileNotFoundError as exc:
        path.unlink(missing_ok=True)
        raise RuntimeError("adb is unavailable until Android SDK setup installs platform-tools; enable the licence option and run sdk_setup") from exc
    except Exception:
        path.unlink(missing_ok=True)
        raise
    if data.returncode:
        path.unlink(missing_ok=True)
        raise RuntimeError(data.stderr.decode(errors="replace")[-2000:])
    with Image.open(path) as im:
        im.thumbnail((1280, 1280))
        buf = BytesIO(); im.save(buf, format="PNG")
    for old in sorted(SCREENSHOTS.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)[50:]: old.unlink()
    return {"path": str(path), "data": base64.b64encode(buf.getvalue()).decode()}
