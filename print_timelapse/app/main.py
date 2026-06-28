import os
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import ha_automations
import settings

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
CURRENT_DIR = DATA_DIR / "current"
ARCHIVE_DIR = DATA_DIR / "archive"
CURRENT_DIR.mkdir(parents=True, exist_ok=True)
ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

# GIF build options (fps, width, cleanup, export path) are read live from
# `settings` at finish time, so they can be changed on the in-app settings
# page and take effect on the next timelapse without an add-on restart.

# Optional copy of every finished GIF out to mapped network storage, e.g.
# /media/NAS1/Photos and Videos/3D Print Timelapses. Disabled (no copy) when
# the export path is unset, since add-ons only see /media if config.yaml
# requests that map.
MEDIA_DIR = Path(os.environ.get("MEDIA_DIR", "/media"))

# Job ids are used directly as directory/file name components, so they're
# restricted to a safe charset to rule out path traversal.
JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_FRAME_RE = re.compile(r"frame_(\d+)\.jpg$")

APP_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Print Timelapse")
app.mount("/archive", StaticFiles(directory=str(ARCHIVE_DIR)), name="archive")
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

# Last time each route was called, for the Help page's "is it working?"
# diagnostic. In-memory only (never persisted) - a timestamp that survived an
# add-on restart would misleadingly look current, so this always means "since
# this add-on last started".
_last_call_at: dict[str, str | None] = {"start": None, "frame": None, "finish": None}


def _record_call(route: str) -> None:
    _last_call_at[route] = datetime.now().isoformat(timespec="seconds")


def _validate_job_id(job_id: str) -> str:
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(
            400, "job_id must contain only letters, numbers, '_' and '-' (max 128 chars)"
        )
    return job_id


def _fetch_image(url: str) -> bytes:
    # Home Assistant's rest_command can't upload local files, so the add-on
    # pulls the snapshot itself instead (e.g. from HA's unauthenticated
    # /local/ static file server) rather than receiving it as an upload.
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return resp.read()
    except (urllib.error.URLError, ValueError) as exc:
        raise HTTPException(502, f"could not fetch image from '{url}': {exc}")


def _local_path_for_url(url: str) -> str:
    """The `camera.snapshot` filename Home Assistant should write to, given
    the /local/<name> URL the customizer form's snapshot_image_url points
    at - the inverse of the /local/ <-> /config/www/ convention DOCS.md
    documents for making snapshot images fetchable by /frame."""
    name = Path(urllib.parse.urlparse(url).path).name
    return f"/config/www/{name}"


def _export_gif(gif_path: Path, gif_name: str) -> str | None:
    export_rel = settings.get_str("gif_export_path")
    if not export_rel:
        return None
    export_dir = MEDIA_DIR / export_rel
    try:
        export_dir.mkdir(parents=True, exist_ok=True)
        export_path = export_dir / gif_name
        shutil.copy2(gif_path, export_path)
        return str(export_path)
    except OSError as exc:
        print(f"[timelapse] failed to export '{gif_name}' to '{export_dir}': {exc}")
        return None


def _gif_records():
    records = []
    for path in sorted(ARCHIVE_DIR.glob("*.gif"), key=lambda p: p.stat().st_mtime, reverse=True):
        stat = path.stat()
        records.append(
            {
                "filename": path.name,
                "url": f"/archive/{path.name}",
                "size_bytes": stat.st_size,
                "created": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
            }
        )
    return records


def _job_records():
    """Capture jobs currently in progress - one per directory under
    `current/` that has at least one frame. `percent` is the highest frame
    number seen so far (frames are named by zero-padded percent)."""
    records = []
    for job_dir in sorted(CURRENT_DIR.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True):
        if not job_dir.is_dir():
            continue
        frames = sorted(job_dir.glob("frame_*.jpg"))
        if not frames:
            continue
        match = _FRAME_RE.search(frames[-1].name)
        percent = int(match.group(1)) if match else 0
        records.append(
            {
                "job_id": job_dir.name,
                "frames": len(frames),
                "percent": percent,
                "latest_frame": frames[-1].name,
                "updated": datetime.fromtimestamp(job_dir.stat().st_mtime).isoformat(timespec="seconds"),
            }
        )
    return records


@app.post("/start")
async def start_job(job_id: str = Form(...)):
    job_id = _validate_job_id(job_id)
    _record_call("start")
    job_dir = CURRENT_DIR / job_id
    if job_dir.exists():
        shutil.rmtree(job_dir)
    job_dir.mkdir(parents=True)
    return {"status": "ok", "job_id": job_id}


@app.post("/frame")
async def add_frame(
    job_id: str = Form(...),
    percent: int = Form(...),
    image_url: str = Form(...),
):
    job_id = _validate_job_id(job_id)
    if not 0 <= percent <= 100:
        raise HTTPException(400, "percent must be between 0 and 100")
    _record_call("frame")

    contents = _fetch_image(image_url)
    if not contents:
        raise HTTPException(400, "fetched image is empty")

    job_dir = CURRENT_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    # Zero-padded percent as the filename: repeat calls at the same percent
    # overwrite (natural dedupe), and lexical sort order matches frame order.
    filename = f"frame_{percent:03d}.jpg"
    (job_dir / filename).write_bytes(contents)

    return {"status": "ok", "job_id": job_id, "filename": filename}


@app.post("/finish")
async def finish_job(job_id: str = Form(...)):
    job_id = _validate_job_id(job_id)
    _record_call("finish")
    job_dir = CURRENT_DIR / job_id

    frames = sorted(job_dir.glob("frame_*.jpg")) if job_dir.exists() else []
    if not frames:
        raise HTTPException(404, f"no frames found for job_id '{job_id}'")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    gif_name = f"{job_id}_{timestamp}.gif"
    gif_path = ARCHIVE_DIR / gif_name

    cmd = [
        "ffmpeg",
        "-y",
        "-framerate",
        str(settings.get_int("gif_fps")),
        # glob (not a numbered %03d pattern) because percent steps can skip
        # values; glob still sorts the zero-padded names into the right order.
        "-pattern_type",
        "glob",
        "-i",
        str(job_dir / "frame_*.jpg"),
        "-vf",
        f"scale={settings.get_int('gif_width')}:-1:flags=lanczos",
        str(gif_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise HTTPException(500, f"ffmpeg failed: {result.stderr[-2000:]}")

    exported_to = _export_gif(gif_path, gif_name)

    if settings.get_bool("cleanup_after_finish"):
        shutil.rmtree(job_dir, ignore_errors=True)

    return {
        "status": "ok",
        "job_id": job_id,
        "filename": gif_name,
        "path": str(gif_path),
        "url": f"/archive/{gif_name}",
        "exported_to": exported_to,
    }


@app.get("/gifs")
async def list_gifs():
    return {"gifs": _gif_records()}


@app.get("/jobs")
async def list_jobs():
    # Polled by the gallery to show capture jobs in progress live.
    return {"jobs": _job_records()}


@app.post("/jobs/delete")
async def delete_job(job_id: str = Form(...)):
    # Clears a capture job's frame directory - used from the gallery to remove
    # a stuck/abandoned "Capturing now" entry (e.g. a job that never got a
    # /finish). Only ever touches `current/`; archived GIFs are untouched.
    job_id = _validate_job_id(job_id)
    job_dir = (CURRENT_DIR / job_id).resolve()
    # Defence in depth on top of the job_id charset check: never delete
    # anything that isn't a direct child of current/.
    if job_dir.parent != CURRENT_DIR.resolve():
        raise HTTPException(400, "invalid job_id")
    shutil.rmtree(job_dir, ignore_errors=True)
    return {"status": "ok", "job_id": job_id}


@app.get("/jobs/{job_id}/frame")
async def latest_job_frame(job_id: str):
    # Serves the most recent frame of an in-progress job, so the gallery's
    # "Capturing now" card can show a live preview of the print. Always the
    # newest frame for that job - no caller-supplied filename - and confined
    # to current/<job_id> by the job_id charset check.
    job_id = _validate_job_id(job_id)
    job_dir = CURRENT_DIR / job_id
    frames = sorted(job_dir.glob("frame_*.jpg")) if job_dir.is_dir() else []
    if not frames:
        raise HTTPException(404, f"no frames for job_id '{job_id}'")
    return FileResponse(frames[-1], media_type="image/jpeg")


@app.get("/", response_class=HTMLResponse)
async def gallery(request: Request):
    gifs = _gif_records()
    context = {
        "gifs": gifs,
        "jobs": _job_records(),
        "total_bytes": sum(g["size_bytes"] for g in gifs),
        "latest": gifs[0]["created"] if gifs else None,
    }
    response = templates.TemplateResponse(request, "gallery.html", context)
    # Without this, a browser tab left open across an add-on rebuild can keep
    # serving its cached copy of the page (and its JS) indefinitely.
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, saved: int = 0):
    response = templates.TemplateResponse(
        request, "settings.html", {"settings": settings.all_settings(), "saved": bool(saved)}
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/settings")
async def save_settings(
    gif_fps: int = Form(...),
    gif_width: int = Form(...),
    cleanup_after_finish: bool = Form(False),
    gif_export_path: str = Form(""),
):
    settings.set_many({
        "gif_fps": gif_fps,
        "gif_width": gif_width,
        "cleanup_after_finish": cleanup_after_finish,
        "gif_export_path": gif_export_path,
    })
    return RedirectResponse("settings?saved=1", status_code=303)


@app.get("/help", response_class=HTMLResponse)
async def help_page(request: Request):
    response = templates.TemplateResponse(
        request,
        "help.html",
        {
            "settings": settings.all_settings(),
            "ha_configured": ha_automations.configured(),
            "default_hostname": ha_automations._DEFAULT_HOSTNAME,
            "last_call_at": _last_call_at,
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/help/status")
async def help_status():
    return {"last_call_at": _last_call_at}


@app.get("/help/hostname")
async def help_hostname():
    if ha_automations.configured():
        hostname = ha_automations.detect_hostname()
        if hostname:
            return {"detected": True, "hostname": hostname}
    return {"detected": False, "hostname": ha_automations._DEFAULT_HOSTNAME}


@app.post("/help/create-automations")
async def help_create_automations(
    print_status_entity: str = Form(...),
    print_progress_entity: str = Form(...),
    snapshot_camera_entity: str = Form(...),
    snapshot_image_url: str = Form(...),
):
    print_status_entity = print_status_entity.strip()
    print_progress_entity = print_progress_entity.strip()
    snapshot_camera_entity = snapshot_camera_entity.strip()
    snapshot_image_url = snapshot_image_url.strip()
    if not all((print_status_entity, print_progress_entity, snapshot_camera_entity, snapshot_image_url)):
        raise HTTPException(400, "all fields are required")

    # Persisted unconditionally (even if Home Assistant isn't reachable
    # below), so the customizer form keeps its values across a reload.
    settings.set_many({
        "print_status_entity": print_status_entity,
        "print_progress_entity": print_progress_entity,
        "snapshot_camera_entity": snapshot_camera_entity,
        "snapshot_image_url": snapshot_image_url,
    })

    if not ha_automations.configured():
        raise HTTPException(503, "Home Assistant API access isn't available - restart this add-on after upgrading")

    return ha_automations.create_automations(
        print_status_entity,
        print_progress_entity,
        snapshot_camera_entity,
        _local_path_for_url(snapshot_image_url),
    )
