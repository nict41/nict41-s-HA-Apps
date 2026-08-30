"""Background ZIM downloads, straight onto the NAS share.

ZIM archives run from a few hundred megabytes to well over 100 GB, so every
transfer here is written incrementally to a `<name>.zim.part` file and picked
back up with a Range request when it is interrupted - by a cancel, an add-on
restart, or the share dropping out mid-copy. Only the completed rename to
`<name>.zim` publishes an archive to the library, so a partial file can never
be mistaken for a usable one.
"""

import queue
import threading
import time
import uuid
from pathlib import Path

import httpx

import library
import settings

CHUNK_SIZE = 1024 * 1024
_TIMEOUT = httpx.Timeout(60.0, connect=15.0)

# Jobs by id, plus the lock guarding them. Everything the UI reads goes
# through snapshot(), which copies under the lock.
_jobs: dict[str, dict] = {}
_lock = threading.RLock()
_queue: "queue.Queue[str]" = queue.Queue()
_workers: list[threading.Thread] = []
_workers_lock = threading.Lock()

ACTIVE_STATES = ("queued", "downloading")


def _now() -> float:
    return time.monotonic()


def _ensure_workers() -> None:
    with _workers_lock:
        if _workers:
            return
        for index in range(settings.MAX_CONCURRENT_DOWNLOADS):
            thread = threading.Thread(target=_worker, name=f"kiwix-download-{index}", daemon=True)
            thread.start()
            _workers.append(thread)


def snapshot() -> list[dict]:
    with _lock:
        jobs = [dict(job) for job in _jobs.values()]
    jobs.sort(key=lambda job: job["created_at"], reverse=True)
    return jobs


def get(job_id: str) -> dict | None:
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def _update(job_id: str, **fields) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is not None:
            job.update(fields)


def active_count() -> int:
    with _lock:
        return sum(1 for job in _jobs.values() if job["status"] in ACTIVE_STATES)


def start(url: str, filename: str, title: str = "", size: int | None = None) -> tuple[dict | None, str]:
    """Queue a download. Returns (job, error_message)."""
    if not url.startswith(("http://", "https://")):
        return None, "That download link isn't a valid HTTP(S) URL."
    if not filename.endswith(".zim") or filename != Path(filename).name:
        return None, "That download doesn't look like a .zim file."

    status = settings.storage_status()
    if not status["ok"]:
        return None, status["message"]

    destination = Path(status["path"]) / filename
    if destination.exists():
        return None, f"{filename} is already in your library."

    with _lock:
        for job in list(_jobs.values()):
            if job["filename"] != filename:
                continue
            if job["status"] in ACTIVE_STATES:
                return None, f"{filename} is already downloading."
            # A finished, failed or interrupted attempt at the same file is
            # superseded by this one rather than lingering beside it; its
            # .part file (if any) is what makes the retry a resume.
            del _jobs[job["id"]]

        free = status.get("free_bytes")
        if size and free is not None and size > free:
            return None, (
                f"Not enough space: {filename} needs {size / 1e9:.1f} GB but only "
                f"{free / 1e9:.1f} GB is free on the share."
            )

        job_id = uuid.uuid4().hex[:12]
        job = {
            "id": job_id,
            "filename": filename,
            "title": title or filename,
            "url": url,
            "total": size,
            "downloaded": 0,
            "status": "queued",
            "error": "",
            "speed": 0.0,
            "created_at": time.time(),
        }
        _jobs[job_id] = job

    _ensure_workers()
    _queue.put(job_id)
    return dict(job), ""


def cancel(job_id: str) -> tuple[bool, str]:
    """Stop a running download, keeping its partial file so it can resume."""
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return False, "No such download."
        if job["status"] not in ACTIVE_STATES:
            return False, "That download isn't running."
        job["cancel"] = True
        if job["status"] == "queued":
            job["status"] = "paused"
    return True, ""


def resume(job_id: str) -> tuple[bool, str]:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return False, "No such download."
        if job["status"] in ACTIVE_STATES:
            return False, "That download is already running."
        job["status"] = "queued"
        job["error"] = ""
        job.pop("cancel", None)

    _ensure_workers()
    _queue.put(job_id)
    return True, ""


def forget(job_id: str) -> tuple[bool, str]:
    """Drop a finished/failed job from the list, leaving files alone."""
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return False, "No such download."
        if job["status"] in ACTIVE_STATES:
            return False, "Cancel the download before removing it."
        del _jobs[job_id]
    return True, ""


def _worker() -> None:
    while True:
        job_id = _queue.get()
        try:
            _run(job_id)
        except Exception as exc:  # noqa: BLE001 - a worker must never die
            _update(job_id, status="error", error=str(exc), speed=0.0)
            print(f"[kiwix] download {job_id} failed: {exc}")
        finally:
            _queue.task_done()


def _run(job_id: str) -> None:
    job = get(job_id)
    if job is None or job.get("cancel"):
        return

    status = settings.storage_status()
    if not status["ok"]:
        _update(job_id, status="error", error=status["message"])
        return

    destination = Path(status["path"]) / job["filename"]
    partial = destination.with_name(destination.name + library.PART_SUFFIX)
    resume_from = partial.stat().st_size if partial.exists() else 0

    _update(job_id, status="downloading", downloaded=resume_from, error="")

    headers = {"Range": f"bytes={resume_from}-"} if resume_from else {}
    try:
        with httpx.stream(
            "GET", job["url"], headers=headers, timeout=_TIMEOUT, follow_redirects=True
        ) as response:
            if response.status_code == 416:
                # The partial file is already the whole thing.
                _finish(job_id, partial, destination)
                return
            response.raise_for_status()

            if resume_from and response.status_code != 206:
                # The mirror ignored the Range header, so the body starts from
                # zero and the partial file has to go.
                resume_from = 0
                _update(job_id, downloaded=0)

            total = _total_size(response, resume_from)
            if total:
                _update(job_id, total=total)

            mode = "ab" if resume_from else "wb"
            written = resume_from
            last_sample = (_now(), written)

            with partial.open(mode) as handle:
                for chunk in response.iter_bytes(CHUNK_SIZE):
                    current = get(job_id)
                    if current is None or current.get("cancel"):
                        handle.flush()
                        _update(job_id, status="paused", speed=0.0)
                        return
                    handle.write(chunk)
                    written += len(chunk)

                    elapsed = _now() - last_sample[0]
                    if elapsed >= 1.0:
                        _update(
                            job_id,
                            downloaded=written,
                            speed=(written - last_sample[1]) / elapsed,
                        )
                        last_sample = (_now(), written)

            _update(job_id, downloaded=written)
    except httpx.HTTPError as exc:
        _update(job_id, status="error", error=f"Download failed: {exc}", speed=0.0)
        return
    except OSError as exc:
        _update(
            job_id,
            status="error",
            speed=0.0,
            error=(
                f"Writing to the share failed: {exc}. If the share was "
                "unmounted, remount it and resume - the partial file is kept."
            ),
        )
        return

    _finish(job_id, partial, destination)


def _total_size(response: httpx.Response, resume_from: int) -> int | None:
    """Full size of the archive, from either Content-Range or Content-Length."""
    content_range = response.headers.get("content-range", "")
    if "/" in content_range:
        try:
            return int(content_range.rsplit("/", 1)[1])
        except ValueError:
            pass
    length = response.headers.get("content-length")
    if length and length.isdigit():
        return int(length) + resume_from
    return None


def _finish(job_id: str, partial: Path, destination: Path) -> None:
    try:
        partial.replace(destination)
    except OSError as exc:
        _update(job_id, status="error", error=f"Could not finalise the download: {exc}", speed=0.0)
        return

    _update(
        job_id,
        status="done",
        speed=0.0,
        downloaded=destination.stat().st_size if destination.exists() else 0,
    )
    print(f"[kiwix] downloaded {destination.name}")

    if settings.AUTO_SERVE_NEW:
        library.set_served(destination.name, True)


def adopt_partials() -> None:
    """Re-list interrupted downloads after an add-on restart.

    A `.part` file on the share is the record that a download was in flight;
    the URL it came from is recovered from the catalog's own naming scheme
    only if the caller supplied one, so restored jobs stay paused until the
    user resumes them (which re-reads the URL from the catalog entry).
    """
    status = settings.storage_status()
    if not status["ok"]:
        return
    for path in Path(status["path"]).glob(f"*.zim{library.PART_SUFFIX}"):
        filename = path.name[: -len(library.PART_SUFFIX)]
        with _lock:
            if any(job["filename"] == filename for job in _jobs.values()):
                continue
            job_id = uuid.uuid4().hex[:12]
            _jobs[job_id] = {
                "id": job_id,
                "filename": filename,
                "title": filename,
                "url": "",
                "total": None,
                "downloaded": path.stat().st_size,
                "status": "interrupted",
                "error": (
                    "This download was interrupted. Find the same archive in "
                    "Browse and start it again - it picks up where it left off."
                ),
                "speed": 0.0,
                "created_at": path.stat().st_mtime,
            }
