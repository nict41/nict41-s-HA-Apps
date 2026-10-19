"""Headless slicing service for the ElegooSlicer add-on.

Exposes the slicer's command line over HTTP so something else on the
Supervisor network (Home Assistant, a script, an assistant) can turn a model
into G-code with named presets, without anyone driving the desktop.

Why presets are flattened here rather than handed to the slicer as they are:
a user preset exported from the GUI is a diff. It holds only the settings
that differ from the system preset named in `inherits`, and the GUI fills in
the rest from its preset collection when it loads. The command line does not
reliably do that for files passed with --load-settings, so a diff loaded on
its own silently slices with defaults for everything it leaves out. Walking
the `inherits` chain here and writing out one complete file per preset makes
the result independent of that behaviour.

Standard library only, so it runs on the image's own python3 with nothing
installed.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
import zipfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

LOG = logging.getLogger("slice-api")

PRESET_TYPES = ("machine", "process", "filament")

# Keys that tie a process or filament preset to specific printers or
# processes. The command line refuses a combination these do not allow, and a
# user's machine preset never appears in a system preset's list, so they are
# cleared: the caller choosing the combination is the point.
COMPAT_KEYS = (
    "compatible_printers",
    "compatible_printers_condition",
    "compatible_prints",
    "compatible_prints_condition",
)

MAX_INHERIT_DEPTH = 20


class SliceError(Exception):
    """A request that cannot be served, with the HTTP status to report."""

    def __init__(self, message: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST):
        super().__init__(message)
        self.status = status


# ── Settings ────────────────────────────────────────────────────────────────


def _paths(value: str) -> list[Path]:
    return [Path(p) for p in value.split(":") if p]


class Settings:
    """Everything configurable, read from the environment once."""

    def __init__(self, env: dict[str, str] | None = None):
        env = dict(os.environ if env is None else env)
        self.port = int(env.get("SLICE_API_PORT", "8100"))
        self.token = env.get("SLICE_API_TOKEN", "")
        self.slicer = env.get("SLICER_BIN", "/opt/elegoo-slicer/AppRun")
        self.system_profiles = _paths(
            env.get("SLICER_SYSTEM_PROFILES", "/opt/elegoo-slicer/resources/profiles")
        )
        # Earlier directories win when two presets share a name.
        self.user_profiles = _paths(
            env.get(
                "SLICER_USER_PROFILES",
                "/share/elegoo_slicer/profiles:/config/.config/ElegooSlicer/user",
            )
        )
        self.allowed_roots = _paths(env.get("SLICE_ALLOWED_ROOTS", "/share:/media"))
        self.work_dir = Path(env.get("SLICE_WORK_DIR", "/tmp/slice-api"))
        self.timeout = int(env.get("SLICE_TIMEOUT", "1800"))
        self.display = env.get("DISPLAY", ":1")


# ── Presets ─────────────────────────────────────────────────────────────────


def _infer_type(path: Path, data: dict[str, Any]) -> str | None:
    declared = data.get("type")
    if declared in PRESET_TYPES:
        return declared
    if declared is not None:
        # machine_model, vendor index files and the like: not presets.
        return None
    # User presets carry no "type"; the GUI files them by folder instead.
    for part in reversed(path.parts[:-1]):
        if part in PRESET_TYPES:
            return part
    return None


class PresetIndex:
    """Every preset on disk, by type and name."""

    def __init__(self, system_roots: list[Path], user_roots: list[Path]):
        # (type, name) -> (path, is_user)
        self._presets: dict[tuple[str, str], tuple[Path, bool]] = {}
        # System first so user presets of the same name replace them, and
        # user roots in reverse so the first-listed root wins.
        for root in system_roots:
            self._scan(root, is_user=False)
        for root in reversed(user_roots):
            self._scan(root, is_user=True)

    def _scan(self, root: Path, *, is_user: bool) -> None:
        if not root.is_dir():
            return
        for path in sorted(root.rglob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict) or not isinstance(data.get("name"), str):
                continue
            kind = _infer_type(path, data)
            if kind is None:
                continue
            self._presets[(kind, data["name"])] = (path, is_user)

    def names(self, kind: str, contains: str = "") -> list[dict[str, Any]]:
        needle = contains.lower()
        out = []
        for (k, name), (path, is_user) in self._presets.items():
            if k != kind or needle not in name.lower():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            # Abstract system presets are only parents; nobody slices with them.
            if not is_user and data.get("instantiation") == "false":
                continue
            out.append({"name": name, "user": is_user})
        return sorted(out, key=lambda p: (not p["user"], p["name"]))

    def load(self, kind: str, name: str) -> dict[str, Any]:
        try:
            path, _ = self._presets[(kind, name)]
        except KeyError:
            raise SliceError(f"no {kind} preset named {name!r}", HTTPStatus.NOT_FOUND) from None
        return json.loads(path.read_text(encoding="utf-8"))

    def resolve(self, kind: str, name: str) -> dict[str, Any]:
        """Return the preset with its whole inherits chain merged in."""
        chain = []
        seen = set()
        current: str | None = name
        while current:
            if current in seen:
                raise SliceError(f"{kind} preset {name!r} inherits from itself via {current!r}")
            if len(chain) >= MAX_INHERIT_DEPTH:
                raise SliceError(f"{kind} preset {name!r} inherits more than {MAX_INHERIT_DEPTH} deep")
            seen.add(current)
            data = self.load(kind, current)
            chain.append(data)
            current = data.get("inherits") or None

        merged: dict[str, Any] = {}
        for data in reversed(chain):
            merged.update(data)
        merged["name"] = name
        merged["type"] = kind
        merged["from"] = "User"
        merged["inherits"] = ""
        merged["instantiation"] = "true"
        if kind != "machine":
            for key in COMPAT_KEYS:
                merged.pop(key, None)
        return merged


# ── Slicing ─────────────────────────────────────────────────────────────────

_TIME_RE = re.compile(r"^; estimated printing time \(normal mode\) = (.+)$", re.M)
_STAT_RES = {
    "filament_g": re.compile(r"^; total filament used \[g\] = ([\d.]+)", re.M),
    "filament_mm": re.compile(r"^; filament used \[mm\] = ([\d.,\s]+)$", re.M),
    "layers": re.compile(r"^; total layer number: (\d+)", re.M),
    "max_z_mm": re.compile(r"^; max_z_height: ([\d.]+)", re.M),
}


def _duration_seconds(text: str) -> int | None:
    units = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    parts = re.findall(r"(\d+)\s*([dhms])", text)
    return sum(int(n) * units[u] for n, u in parts) if parts else None


def gcode_stats(gcode: Path) -> dict[str, Any]:
    """Pull the headline numbers out of a sliced file.

    The header and footer are where the slicer writes them; the middle of a
    multi-megabyte file is never read.
    """
    size = gcode.stat().st_size
    with gcode.open("rb") as fh:
        head = fh.read(64 * 1024)
        fh.seek(max(0, size - 256 * 1024))
        tail = fh.read()
    text = (head + b"\n" + tail).decode("utf-8", errors="replace")

    stats: dict[str, Any] = {"bytes": size}
    if m := _TIME_RE.search(text):
        stats["print_time"] = m.group(1).strip()
        stats["print_time_s"] = _duration_seconds(m.group(1))
    for key, rx in _STAT_RES.items():
        if m := rx.search(text):
            raw = m.group(1).strip()
            if key == "filament_mm":
                stats[key] = [float(v) for v in raw.split(",") if v.strip()]
            elif key == "layers":
                stats[key] = int(raw)
            else:
                stats[key] = float(raw)
    has_thumbnail = "; thumbnail begin" in text
    stats["thumbnail"] = has_thumbnail
    return stats


def _inside(path: Path, roots: list[Path]) -> bool:
    resolved = path.resolve()
    return any(resolved.is_relative_to(root.resolve()) for root in roots)


def _safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._ -]+", "_", text).strip() or "model"


class Slicer:
    """Runs one slice at a time; the slicer is too heavy to run in parallel."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = threading.Lock()

    def index(self) -> PresetIndex:
        # Rebuilt per request so a preset dropped into /share is picked up
        # without restarting anything. A few hundred small files: cheap.
        return PresetIndex(self.settings.system_profiles, self.settings.user_profiles)

    def _check_path(self, raw: Any, what: str, *, must_exist: bool) -> Path:
        if not isinstance(raw, str) or not raw:
            raise SliceError(f"{what} must be a path")
        path = Path(raw)
        if not path.is_absolute():
            raise SliceError(f"{what} must be an absolute path")
        if not _inside(path, self.settings.allowed_roots):
            roots = ", ".join(str(r) for r in self.settings.allowed_roots)
            raise SliceError(f"{what} must be inside one of: {roots}", HTTPStatus.FORBIDDEN)
        if must_exist and not path.is_file():
            raise SliceError(f"{what} {raw} does not exist", HTTPStatus.NOT_FOUND)
        return path

    def build_command(
        self,
        job: Path,
        model: Path,
        settings_files: list[Path],
        filament_files: list[Path],
        request: dict[str, Any],
    ) -> list[str]:
        cmd = [
            self.settings.slicer,
            "--datadir", str(job / "datadir"),
            "--load-settings", ";".join(str(p) for p in settings_files),
            "--load-filaments", ";".join(str(p) for p in filament_files),
            "--slice", str(int(request.get("plate", 0))),
            "--outputdir", str(job / "out"),
            "--export-3mf", str(job / "out" / "result.3mf"),
        ]
        if request.get("arrange"):
            cmd += ["--arrange", "1"]
        if request.get("orient"):
            cmd += ["--orient", "1"]
        if bed := request.get("bed_type"):
            cmd += ["--curr-bed-type", str(bed)]
        cmd.append(str(model))
        return cmd

    def _collect_gcode(self, out: Path) -> Path | None:
        found = sorted(out.glob("*.gcode"))
        if found:
            return found[0]
        archive = out / "result.3mf"
        if not archive.is_file():
            return None
        with zipfile.ZipFile(archive) as zf:
            names = sorted(n for n in zf.namelist() if re.fullmatch(r"Metadata/plate_\d+\.gcode", n))
            if not names:
                return None
            target = out / Path(names[0]).name
            with zf.open(names[0]) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            return target

    def slice(self, request: dict[str, Any]) -> dict[str, Any]:
        model = self._check_path(request.get("model"), "model", must_exist=True)
        machine = request.get("machine")
        process = request.get("process")
        filaments = request.get("filaments")
        if isinstance(filaments, str):
            filaments = [filaments]
        if not isinstance(machine, str) or not isinstance(process, str):
            raise SliceError("machine and process must be preset names")
        if not filaments or not all(isinstance(f, str) for f in filaments):
            raise SliceError("filaments must be a list of preset names, one per slot")

        overrides = request.get("overrides") or {}
        if not isinstance(overrides, dict):
            raise SliceError("overrides must be an object of {machine|process|filament: {key: value}}")

        if request.get("output"):
            output = self._check_path(request["output"], "output", must_exist=False)
            # Whatever is at this path gets replaced, so it can only ever be
            # a G-code file, never a model, a config or anything else on /share.
            if output.suffix.lower() != ".gcode":
                raise SliceError("output must end in .gcode")
        else:
            output = model.with_suffix(".gcode")

        index = self.index()
        resolved = {
            "machine": index.resolve("machine", machine),
            "process": index.resolve("process", process),
        }
        resolved_filaments = [index.resolve("filament", f) for f in filaments]
        for kind in ("machine", "process"):
            resolved[kind].update(overrides.get(kind) or {})
        for fil in resolved_filaments:
            fil.update(overrides.get("filament") or {})

        if not self._lock.acquire(blocking=False):
            raise SliceError("a slice is already running", HTTPStatus.CONFLICT)
        job = self.settings.work_dir / f"job-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        try:
            (job / "out").mkdir(parents=True)
            (job / "datadir").mkdir()
            files = []
            for kind in ("machine", "process"):
                path = job / f"{kind}.json"
                path.write_text(json.dumps(resolved[kind], indent=1), encoding="utf-8")
                files.append(path)
            fil_files = []
            for i, fil in enumerate(resolved_filaments, start=1):
                path = job / f"filament_{i}.json"
                path.write_text(json.dumps(fil, indent=1), encoding="utf-8")
                fil_files.append(path)

            cmd = self.build_command(job, model, files, fil_files, request)
            env = dict(os.environ, DISPLAY=self.settings.display)
            started = time.monotonic()
            LOG.info("slicing %s with %s / %s / %s", model, machine, process, filaments)
            try:
                proc = subprocess.run(
                    cmd, cwd=job, env=env, capture_output=True, text=True,
                    timeout=self.settings.timeout, check=False,
                )
            except subprocess.TimeoutExpired:
                raise SliceError(
                    f"slicer did not finish within {self.settings.timeout}s",
                    HTTPStatus.GATEWAY_TIMEOUT,
                ) from None
            elapsed = round(time.monotonic() - started, 1)
            log = (proc.stdout or "") + (proc.stderr or "")
            (job / "slicer.log").write_text(log, encoding="utf-8")

            gcode = self._collect_gcode(job / "out")
            result: dict[str, Any] = {
                "ok": proc.returncode == 0 and gcode is not None,
                "returncode": proc.returncode,
                "seconds": elapsed,
                "job_dir": str(job),
                "command": cmd,
                "log_tail": log[-4000:],
            }
            if gcode is not None:
                output.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(gcode, output)
                result["gcode"] = str(output)
                result["stats"] = gcode_stats(output)
            return result
        finally:
            self._lock.release()


# ── HTTP ────────────────────────────────────────────────────────────────────


def make_handler(slicer: Slicer) -> type[BaseHTTPRequestHandler]:
    settings = slicer.settings

    class Handler(BaseHTTPRequestHandler):
        server_version = "ElegooSliceAPI/1"

        def log_message(self, fmt: str, *args: Any) -> None:
            LOG.info("%s %s", self.address_string(), fmt % args)

        def _send(self, status: HTTPStatus, body: dict[str, Any]) -> None:
            data = json.dumps(body, indent=1).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorised(self) -> bool:
            if not settings.token:
                return True
            return self.headers.get("Authorization", "") == f"Bearer {settings.token}"

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 1024 * 1024:
                raise SliceError("request body too large")
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                raise SliceError("request body is not JSON") from None
            if not isinstance(body, dict):
                raise SliceError("request body must be a JSON object")
            return body

        def _dispatch(self, method: str) -> None:
            if not self._authorised():
                self._send(HTTPStatus.UNAUTHORIZED, {"error": "missing or wrong bearer token"})
                return
            url = urlparse(self.path)
            query = {k: v[-1] for k, v in parse_qs(url.query).items()}
            try:
                if method == "GET" and url.path == "/health":
                    body = {
                        "ok": True,
                        "slicer": settings.slicer,
                        "slicer_present": Path(settings.slicer).exists(),
                        "system_profiles": [str(p) for p in settings.system_profiles if p.is_dir()],
                        "user_profiles": [str(p) for p in settings.user_profiles if p.is_dir()],
                        "busy": slicer._lock.locked(),
                    }
                elif method == "GET" and url.path == "/presets":
                    kind = query.get("type", "")
                    if kind not in PRESET_TYPES:
                        raise SliceError(f"type must be one of {', '.join(PRESET_TYPES)}")
                    body = {"presets": slicer.index().names(kind, query.get("contains", ""))}
                elif method == "GET" and url.path == "/resolve":
                    kind = query.get("type", "")
                    if kind not in PRESET_TYPES or not query.get("name"):
                        raise SliceError("type and name are required")
                    body = slicer.index().resolve(kind, query["name"])
                elif method == "POST" and url.path == "/slice":
                    body = slicer.slice(self._body())
                else:
                    raise SliceError(f"no route {method} {url.path}", HTTPStatus.NOT_FOUND)
            except SliceError as err:
                self._send(err.status, {"ok": False, "error": str(err)})
                return
            except Exception as err:  # noqa: BLE001 - report, never drop the connection
                LOG.exception("request failed")
                self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": repr(err)})
                return
            self._send(HTTPStatus.OK, body)

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

    return Handler


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="[slice-api] %(message)s")
    settings = Settings()
    slicer = Slicer(settings)
    server = ThreadingHTTPServer(("0.0.0.0", settings.port), make_handler(slicer))
    LOG.info(
        "listening on :%d (%s)", settings.port,
        "bearer token required" if settings.token else "no token configured",
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
