"""Background ZIM downloads, straight onto the NAS share.

ZIM archives run from a few hundred megabytes to well over 100 GB, so nothing
here assumes a transfer will finish in one go. Every download is written to a
`<name>.zim.part` file next to its destination and can be picked back up with
Range requests after a pause, an add-on restart, or the share dropping out
mid-copy. Only the final rename to `<name>.zim` publishes an archive to the
library, so a partial file is never mistaken for a usable one.

Three things layer on top of that:

* **Segments.** A download may be split across several connections, each
  fetching its own byte range into the same file. The plan lives in a
  `<name>.zim.part.json` sidecar beside the data, because the file's own
  length says nothing about which ranges are present once the writes stop
  being sequential. No sidecar means the `.part` is a plain contiguous
  prefix - which is what single-connection downloads (and any download from
  before segments existed) leave behind, so they resume unchanged.
* **A scheduler.** How many archives may transfer at once is read fresh on
  every tick, so changing it in the app applies immediately.
* **A window.** Downloads can be confined to off-peak hours; when the window
  closes, running transfers pause exactly like a manual pause and resume
  themselves when it opens again.
"""

import json
import os
import threading
import time
import uuid
from pathlib import Path

import httpx

import catalog
import library
import settings

CHUNK_SIZE = 1024 * 1024
_TIMEOUT = httpx.Timeout(60.0, connect=15.0)

# Don't split a transfer into pieces smaller than this: past a point the extra
# connections cost more in per-request overhead (and NAS seeks) than they win.
MIN_SEGMENT = 32 * 1024 * 1024

# How often the scheduler re-reads settings, starts queued work and samples
# transfer rates.
TICK_SECONDS = 2.0

# How much progress a segment may make before its position is written to the
# plan. Only what the plan records survives a hard stop (a container killed
# mid-write, a power cut), and the rest is downloaded again - so on a fast
# link a purely time-based interval throws away a surprising amount. The
# sidecar is a few hundred bytes, so saving often is cheap.
PLAN_SAVE_BYTES = 16 * 1024 * 1024
PLAN_SAVE_SECONDS = 2.0

ACTIVE_STATES = ("queued", "downloading", "waiting")

_jobs: dict[str, dict] = {}
_lock = threading.RLock()
_scheduler_started = False


# ── Job registry ────────────────────────────────────────────────────────────

def registry_file() -> Path:
    return settings.DATA_DIR / "downloads.json"


def _persist() -> None:
    """Save the job list so an add-on restart resumes instead of forgetting.

    Only the durable fields are written; live ones (rate, per-segment
    progress) are recomputed from the share on the next start.
    """
    keep = ("id", "filename", "title", "url", "total", "downloaded", "status", "error",
            "created_at", "ignore_window")
    with _lock:
        rows = [{key: job.get(key) for key in keep} for job in _jobs.values()]
    try:
        settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = registry_file().with_suffix(".json.tmp")
        tmp.write_text(json.dumps(rows, indent=2))
        tmp.replace(registry_file())
    except OSError as exc:
        print(f"[kiwix] could not save the download list: {exc}")


def load_registry() -> None:
    """Restore jobs saved before the last restart.

    Anything that was mid-flight comes back as `queued`, so the scheduler
    picks it up again on its own - the bytes already on the share are what
    make that a resume rather than a restart.
    """
    try:
        rows = json.loads(registry_file().read_text())
    except (OSError, ValueError):
        return
    if not isinstance(rows, list):
        return

    # Progress isn't written to the registry on every chunk (that would mean
    # a disk write per megabyte), so the byte count in it is stale by
    # design. The share itself is the authority, and reading it back here is
    # what makes a restored job show its real position straight away.
    status = settings.storage_status()
    directory = Path(status["path"]) if status["ok"] else None

    with _lock:
        for row in rows:
            if not isinstance(row, dict) or not row.get("id"):
                continue
            status = row.get("status", "queued")
            if status in ("downloading", "queued", "waiting"):
                status = "queued"
            _jobs[row["id"]] = {
                "id": row["id"],
                "filename": row.get("filename", ""),
                "title": row.get("title") or row.get("filename", ""),
                "url": row.get("url", ""),
                "total": row.get("total"),
                "downloaded": (
                    _bytes_on_disk(directory, row.get("filename", ""))
                    if directory else (row.get("downloaded") or 0)
                ),
                "status": status,
                "error": row.get("error", ""),
                "speed": 0.0,
                "created_at": row.get("created_at") or time.time(),
                "ignore_window": bool(row.get("ignore_window")),
            }


def snapshot() -> list[dict]:
    with _lock:
        jobs = [dict(job) for job in _jobs.values()]
    for job in jobs:
        job.pop("stop", None)
    jobs.sort(key=lambda job: job["created_at"], reverse=True)
    return jobs


def get(job_id: str) -> dict | None:
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def _update(job_id: str, persist: bool = True, **fields) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job.update(fields)
    if persist:
        _persist()


def active_count() -> int:
    with _lock:
        return sum(1 for job in _jobs.values() if job["status"] == "downloading")


# ── Public actions ──────────────────────────────────────────────────────────

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
            "downloaded": _bytes_on_disk(Path(status["path"]), filename),
            "status": "queued",
            "error": "",
            "speed": 0.0,
            "created_at": time.time(),
            "ignore_window": False,
        }
        _jobs[job_id] = job

    _persist()
    start_scheduler()
    return dict(job), ""


def pause(job_id: str) -> tuple[bool, str]:
    """Stop a running download, keeping its partial file so it can resume."""
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return False, "No such download."
        if job["status"] not in ACTIVE_STATES:
            return False, "That download isn't running."
        job["stop"] = "paused"
        if job["status"] != "downloading":
            job["status"] = "paused"
    _persist()
    return True, ""


# The UI called this "cancel" before pausing and cancelling became the same
# thing; keep the name working for anything still using it.
cancel = pause


def resume(job_id: str, ignore_window: bool = False) -> tuple[bool, str]:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return False, "No such download."
        if job["status"] in ("downloading", "queued"):
            return False, "That download is already running."
        job["status"] = "queued"
        job["error"] = ""
        job.pop("stop", None)
        if ignore_window:
            job["ignore_window"] = True

    _persist()
    start_scheduler()
    return True, ""


def download_now(job_id: str) -> tuple[bool, str]:
    """Let one download ignore the off-peak window, starting immediately."""
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return False, "No such download."
        job["ignore_window"] = True
        if job["status"] in ("waiting", "paused", "error"):
            job["status"] = "queued"
            job["error"] = ""
            job.pop("stop", None)
    _persist()
    start_scheduler()
    return True, ""


def forget(job_id: str) -> tuple[bool, str]:
    """Drop a finished/failed job from the list, leaving files alone."""
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return False, "No such download."
        if job["status"] in ("downloading", "queued"):
            return False, "Pause the download before removing it."
        del _jobs[job_id]
    _persist()
    return True, ""


def adopt_partials() -> None:
    """List `.part` files on the share that no job knows about.

    These are downloads from before the job list was persisted (or from a
    copy made by hand). The bytes are kept; resuming one looks its source up
    in the catalog by filename, so it continues rather than starting over.
    """
    status = settings.storage_status()
    if not status["ok"]:
        return
    for path in Path(status["path"]).glob("*.zim.part"):
        filename = path.name[: -len(".part")]
        with _lock:
            if any(job["filename"] == filename for job in _jobs.values()):
                continue
            job_id = uuid.uuid4().hex[:12]
            _jobs[job_id] = {
                "id": job_id,
                "filename": filename,
                "title": filename[: -len(".zim")].replace("_", " "),
                "url": "",
                "total": None,
                "downloaded": _bytes_on_disk(Path(status["path"]), filename),
                "status": "interrupted",
                "error": "This download was interrupted. Resume picks it up where it stopped.",
                "speed": 0.0,
                "created_at": path.stat().st_mtime,
                "ignore_window": False,
            }
    _persist()


# ── Segment plans ───────────────────────────────────────────────────────────

def _part_paths(directory: Path, filename: str) -> tuple[Path, Path, Path]:
    destination = directory / filename
    part = destination.with_name(destination.name + ".part")
    return destination, part, part.with_name(part.name + ".json")


def _bytes_on_disk(directory: Path, filename: str) -> int:
    """How much of an archive is already downloaded, plan or no plan."""
    _, part, sidecar = _part_paths(directory, filename)
    plan = _read_plan(sidecar)
    if plan:
        return sum(segment["done"] for segment in plan["segments"])
    try:
        return part.stat().st_size
    except OSError:
        return 0


def _read_plan(sidecar: Path) -> dict | None:
    try:
        plan = json.loads(sidecar.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(plan, dict) or not isinstance(plan.get("segments"), list):
        return None
    return plan


def _write_plan(sidecar: Path, plan: dict) -> None:
    tmp = sidecar.with_name(sidecar.name + ".tmp")
    tmp.write_text(json.dumps(plan))
    tmp.replace(sidecar)


def _plan_segments(base: int, total: int, threads: int) -> list[dict]:
    """Split [base, total) across up to `threads` ranges.

    Anything already on disk becomes a leading, already-complete segment, so
    a part-finished sequential download converts to a segmented one without
    re-fetching a byte.
    """
    segments = [{"start": 0, "end": base, "done": base}] if base else []
    remaining = total - base
    count = max(1, min(threads, remaining // MIN_SEGMENT))
    size = remaining // count

    for index in range(count):
        start = base + index * size
        end = total if index == count - 1 else base + (index + 1) * size
        segments.append({"start": start, "end": end, "done": 0})
    return segments


def _resplit(segments: list[dict], threads: int) -> list[dict]:
    """Break up unfetched ranges so a raised thread count takes effect.

    Only the part of a segment that hasn't been downloaded yet is ever split,
    so this can run on resume without invalidating anything already written.
    """
    segments = [dict(segment) for segment in segments]
    while True:
        pending = [s for s in segments if s["done"] < s["end"] - s["start"]]
        if len(pending) >= threads:
            return segments
        widest = max(pending, key=lambda s: s["end"] - s["start"] - s["done"], default=None)
        if widest is None or widest["end"] - widest["start"] - widest["done"] < 2 * MIN_SEGMENT:
            return segments
        cut = widest["start"] + widest["done"] + (widest["end"] - widest["start"] - widest["done"]) // 2
        tail = {"start": cut, "end": widest["end"], "done": 0}
        widest["end"] = cut
        segments.insert(segments.index(widest) + 1, tail)


def _probe(url: str) -> tuple[int | None, bool]:
    """(total size, whether the server honours Range) for a download URL."""
    try:
        response = httpx.head(url, timeout=_TIMEOUT, follow_redirects=True)
        if response.status_code >= 400:
            response = httpx.get(
                url, headers={"Range": "bytes=0-0"}, timeout=_TIMEOUT, follow_redirects=True
            )
    except httpx.HTTPError:
        return None, False

    ranges = response.headers.get("accept-ranges", "").lower() == "bytes"
    length = response.headers.get("content-length")
    content_range = response.headers.get("content-range", "")

    total = None
    if "/" in content_range:
        try:
            total = int(content_range.rsplit("/", 1)[1])
            ranges = True
        except ValueError:
            total = None
    elif length and length.isdigit() and response.status_code == 200:
        total = int(length)

    return total, ranges


# ── Transfers ───────────────────────────────────────────────────────────────

def _stopped(job_id: str) -> str:
    job = get(job_id)
    if job is None:
        return "paused"
    return job.get("stop") or ""


def _run(job_id: str) -> None:
    """Download one archive, in as many pieces as the settings allow."""
    job = get(job_id)
    if job is None or job.get("stop"):
        return

    status = settings.storage_status()
    if not status["ok"]:
        _update(job_id, status="error", error=status["message"], speed=0.0)
        return

    directory = Path(status["path"])
    destination, part, sidecar = _part_paths(directory, job["filename"])
    threads = int(settings.get("download_threads"))

    plan = _read_plan(sidecar)
    if plan is None:
        base = part.stat().st_size if part.exists() else 0
        total, ranges = _probe(job["url"])
        if total is not None:
            _update(job_id, total=total)
        if total is None or not ranges or threads <= 1 or total - base < 2 * MIN_SEGMENT:
            # One connection, appending: the file's own length is the whole
            # story, so no sidecar is written and the result stays readable
            # by any earlier version of this add-on.
            _sequential(job_id, job["url"], part, destination, base, total)
            return
        plan = {"version": 1, "mode": "segmented", "url": job["url"], "total": total,
                "segments": _plan_segments(base, total, threads)}
    else:
        plan["segments"] = _resplit(plan["segments"], threads)
        _update(job_id, total=plan.get("total"))

    _write_plan(sidecar, plan)
    _segmented(job_id, plan, part, destination, sidecar)


def _sequential(job_id: str, url: str, part: Path, destination: Path,
                base: int, total: int | None) -> None:
    headers = {"Range": f"bytes={base}-"} if base else {}
    try:
        with httpx.stream("GET", url, headers=headers, timeout=_TIMEOUT,
                          follow_redirects=True) as response:
            if response.status_code == 416:
                _finish(job_id, part, destination, None)
                return
            response.raise_for_status()

            if base and response.status_code != 206:
                # The mirror ignored the Range header, so the body starts from
                # zero and whatever was on disk has to be written over.
                base = 0

            written = base
            mode = "ab" if base else "wb"
            with part.open(mode) as handle:
                for chunk in response.iter_bytes(CHUNK_SIZE):
                    if _stopped(job_id):
                        handle.flush()
                        _settle(job_id)
                        return
                    handle.write(chunk)
                    written += len(chunk)
                    _update(job_id, persist=False, downloaded=written)
    except httpx.HTTPError as exc:
        _update(job_id, status="error", error=f"Download failed: {exc}", speed=0.0)
        return
    except OSError as exc:
        _update(job_id, status="error", speed=0.0, error=_share_error(exc))
        return

    _finish(job_id, part, destination, None)


def _segmented(job_id: str, plan: dict, part: Path, destination: Path, sidecar: Path) -> None:
    # Create the file if it isn't there; segments then write at their own
    # offsets into it, so it fills in out of order.
    try:
        part.touch(exist_ok=True)
    except OSError as exc:
        _update(job_id, status="error", speed=0.0, error=_share_error(exc))
        return

    failures: list[str] = []
    plan_lock = threading.Lock()
    workers = []

    for index, segment in enumerate(plan["segments"]):
        if segment["done"] >= segment["end"] - segment["start"]:
            continue
        worker = threading.Thread(
            target=_segment_worker,
            args=(job_id, plan, index, part, sidecar, plan_lock, failures),
            name=f"kiwix-seg-{job_id}-{index}",
            daemon=True,
        )
        worker.start()
        workers.append(worker)

    for worker in workers:
        worker.join()

    with plan_lock:
        _write_plan(sidecar, plan)
        complete = all(s["done"] >= s["end"] - s["start"] for s in plan["segments"])

    if _stopped(job_id):
        _settle(job_id)
        return
    if failures:
        _update(job_id, status="error", speed=0.0, error=failures[0])
        return
    if not complete:
        _update(job_id, status="error", speed=0.0,
                error="The download ended before every part was fetched; resume to continue.")
        return

    _finish(job_id, part, destination, sidecar, expected=plan.get("total"))


def _segment_worker(job_id: str, plan: dict, index: int, part: Path, sidecar: Path,
                    plan_lock: threading.Lock, failures: list) -> None:
    segment = plan["segments"][index]
    last_save = time.monotonic()
    saved_at_bytes = segment["done"]

    try:
        # O_WRONLY per worker: each writes only inside its own byte range, so
        # the handles never contend and no locking is needed around the data.
        handle = os.open(part, os.O_WRONLY)
    except OSError as exc:
        failures.append(_share_error(exc))
        return

    try:
        while segment["done"] < segment["end"] - segment["start"]:
            offset = segment["start"] + segment["done"]
            headers = {"Range": f"bytes={offset}-{segment['end'] - 1}"}
            try:
                with httpx.stream("GET", plan["url"], headers=headers, timeout=_TIMEOUT,
                                  follow_redirects=True) as response:
                    if response.status_code not in (200, 206):
                        response.raise_for_status()
                    if response.status_code == 200 and offset:
                        failures.append(
                            "The server stopped honouring range requests, so this "
                            "download can't be split; set download threads to 1 and resume."
                        )
                        return

                    for chunk in response.iter_bytes(CHUNK_SIZE):
                        if _stopped(job_id):
                            return
                        remaining = segment["end"] - segment["start"] - segment["done"]
                        if remaining <= 0:
                            return
                        chunk = chunk[:remaining]
                        os.pwrite(handle, chunk, segment["start"] + segment["done"])
                        with plan_lock:
                            segment["done"] += len(chunk)
                            done = sum(s["done"] for s in plan["segments"])
                        _update(job_id, persist=False, downloaded=done)

                        now = time.monotonic()
                        if (now - last_save >= PLAN_SAVE_SECONDS
                                or segment["done"] - saved_at_bytes >= PLAN_SAVE_BYTES):
                            with plan_lock:
                                _write_plan(sidecar, plan)
                            last_save = now
                            saved_at_bytes = segment["done"]
                return
            except httpx.HTTPError as exc:
                # A dropped connection mid-range is normal on a long transfer;
                # the loop re-requests from wherever this segment got to.
                if _stopped(job_id):
                    return
                print(f"[kiwix] segment {index} of {plan['url']} interrupted: {exc}; retrying")
                time.sleep(2.0)
                if segment["done"] == 0:
                    failures.append(f"Download failed: {exc}")
                    return
    except OSError as exc:
        failures.append(_share_error(exc))
    finally:
        os.close(handle)
        with plan_lock:
            _write_plan(sidecar, plan)


def _share_error(exc: OSError) -> str:
    return (
        f"Writing to the share failed: {exc}. If the share was unmounted, "
        "remount it and resume - the partial file is kept."
    )


def _settle(job_id: str) -> None:
    """Apply whatever stop reason took the transfer out of its loop."""
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job["status"] = job.pop("stop", None) or "paused"
        job["speed"] = 0.0
    _persist()


def _finish(job_id: str, part: Path, destination: Path, sidecar: Path | None,
            expected: int | None = None) -> None:
    try:
        if expected is not None and part.stat().st_size != expected:
            _update(job_id, status="error", speed=0.0,
                    error="The finished file is the wrong size; resume to fetch the rest.")
            return
        part.replace(destination)
        if sidecar is not None:
            sidecar.unlink(missing_ok=True)
    except OSError as exc:
        _update(job_id, status="error", error=f"Could not finalise the download: {exc}", speed=0.0)
        return

    _update(job_id, status="done", speed=0.0,
            downloaded=destination.stat().st_size if destination.exists() else 0)
    print(f"[kiwix] downloaded {destination.name}")

    if settings.get("auto_serve_new"):
        library.set_served(destination.name, True)


# ── Scheduler ───────────────────────────────────────────────────────────────

def _resolve_url(job_id: str) -> bool:
    """Find the catalog entry for an adopted `.part` so it can resume."""
    job = get(job_id)
    if job is None or job["url"]:
        return True
    try:
        entry = catalog.find_by_filename(job["filename"])
    except catalog.CatalogError as exc:
        _update(job_id, status="interrupted",
                error=f"Couldn't look this archive up in the catalog: {exc}")
        return False
    if entry is None:
        _update(job_id, status="interrupted", error=(
            "This archive isn't in the catalog any more (its edition may have "
            "been replaced), so the download can't be resumed. Delete it and "
            "pick the current edition in Browse."))
        return False
    _update(job_id, url=entry["url"], total=entry.get("size"), title=entry.get("title") or job["title"])
    return True


def _launch(job_id: str) -> None:
    _update(job_id, status="downloading", error="")
    threading.Thread(target=_guarded_run, args=(job_id,), name=f"kiwix-dl-{job_id}",
                     daemon=True).start()


def _guarded_run(job_id: str) -> None:
    try:
        _run(job_id)
    except Exception as exc:  # noqa: BLE001 - a transfer must never take the app down
        print(f"[kiwix] download {job_id} failed: {exc}")
        _update(job_id, status="error", error=str(exc), speed=0.0)


def tick(now=None) -> None:
    """One scheduling pass: sample rates, honour the window, fill free slots."""
    values = settings.all_settings()
    window = settings.window_state(now)
    limit = int(values["max_concurrent_downloads"])

    with _lock:
        jobs = list(_jobs.values())
        sampled_at = time.monotonic()
        for job in jobs:
            previous = job.get("_sampled_at")
            if job["status"] == "downloading" and previous:
                elapsed = sampled_at - previous
                if elapsed > 0:
                    job["speed"] = max(0.0, (job["downloaded"] - job.get("_sampled_bytes", 0)) / elapsed)
            elif job["status"] != "downloading":
                job["speed"] = 0.0
            job["_sampled_at"] = sampled_at
            job["_sampled_bytes"] = job["downloaded"]

        # Outside the window, running transfers stand down; they keep their
        # partial files and come back when it reopens.
        if not window["open"]:
            for job in jobs:
                if job["status"] == "downloading" and not job["ignore_window"]:
                    job["stop"] = "waiting"
                elif job["status"] == "queued" and not job["ignore_window"]:
                    job["status"] = "waiting"

        running = sum(1 for job in _jobs.values() if job["status"] == "downloading")
        startable = [
            job for job in sorted(_jobs.values(), key=lambda j: j["created_at"])
            if job["status"] in ("queued", "waiting")
            and (window["open"] or job["ignore_window"])
        ]

    for job in startable:
        if running >= limit:
            break
        if not _resolve_url(job["id"]):
            continue
        _launch(job["id"])
        running += 1


def _scheduler() -> None:
    while True:
        try:
            tick()
        except Exception as exc:  # noqa: BLE001 - the scheduler must not die
            print(f"[kiwix] scheduler error: {exc}")
        time.sleep(TICK_SECONDS)


def start_scheduler() -> None:
    global _scheduler_started
    with _lock:
        if _scheduler_started:
            return
        _scheduler_started = True
    threading.Thread(target=_scheduler, name="kiwix-scheduler", daemon=True).start()
