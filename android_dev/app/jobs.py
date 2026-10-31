import json
import os
import re
import selectors
import signal
import subprocess
import threading
import time
import uuid

from config import DATA, GRADLE_HOME, HEAP_GB, JOB_TIMEOUT, WORKERS
from paths import project_path

JOBS = DATA / "jobs"
JOBS.mkdir(parents=True, exist_ok=True)
LOCK = threading.Lock()
CURRENT = None
RECORDS = {}
REDACT_KEYS = ("ANDROID_SIGNING_STORE_PASSWORD", "ANDROID_SIGNING_KEY_PASSWORD")


def gradle_properties():
    GRADLE_HOME.mkdir(parents=True, exist_ok=True)
    (GRADLE_HOME / "gradle.properties").write_text(
        f"org.gradle.jvmargs=-Xmx{HEAP_GB}g -XX:+UseParallelGC -Dfile.encoding=UTF-8\n"
        f"org.gradle.workers.max={WORKERS}\norg.gradle.parallel=true\norg.gradle.caching=true\n"
        "org.gradle.daemon.idletimeout=600000\n", encoding="utf-8")


def _redact(data: bytes, env: dict) -> bytes:
    for key in REDACT_KEYS:
        val = env.get(key)
        if val:
            data = data.replace(val.encode(), b"[REDACTED]")
    return data


def _drain(stream) -> bytes:
    # Never read to EOF here: a daemon left behind by the build can hold the pipe open.
    os.set_blocking(stream.fileno(), False)
    data = bytearray()
    while True:
        try: chunk = os.read(stream.fileno(), 65536)
        except BlockingIOError: return bytes(data)
        if not chunk: return bytes(data)
        data.extend(chunk)


def _run_job(job_id, command, cwd, env, timeout, record):
    global CURRENT
    log_path = JOBS / f"{job_id}.log"
    started = time.time()
    try:
        with log_path.open("wb") as out:
            proc = subprocess.Popen(["nice", "-n", "10", *command], cwd=cwd, env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    start_new_session=True)
            record.update(process=proc, pid=proc.pid, state="running")
            written = 0
            pending = b""
            secrets = [env[key].encode() for key in REDACT_KEYS if env.get(key)]
            overlap = max((len(secret) for secret in secrets), default=0)

            def emit(chunk, final=False):
                nonlocal pending, written
                pending += chunk
                amount = len(pending) if final else max(0, len(pending) - overlap)
                # Keep any secret prefix that would otherwise straddle the write boundary.
                if not final:
                    changed = True
                    while changed and amount:
                        changed = False
                        for secret in secrets:
                            for size in range(min(len(secret) - 1, amount), 0, -1):
                                if pending[amount - size:amount] == secret[:size]:
                                    amount -= size
                                    changed = True
                                    break
                            if changed: break
                safe = _redact(pending[:amount], env)
                pending = pending[amount:]
                if not safe: return
                remaining = 10 * 1024 * 1024 - written
                if remaining > 0:
                    if len(safe) > remaining: record["log_truncated"] = True
                    safe = safe[:remaining]
                    out.write(safe); written += len(safe); out.flush()
                elif safe: record["log_truncated"] = True

            selector = selectors.DefaultSelector()
            selector.register(proc.stdout, selectors.EVENT_READ)
            with LOCK:
                (JOBS / "state.json").write_text(json.dumps({"running": job_id}))
            try:
                while True:
                    ready = selector.select(timeout=0.1)
                    try: chunk = os.read(proc.stdout.fileno(), 65536) if ready else b""
                    except OSError: chunk = b""
                    if chunk:
                        emit(chunk)
                    elif ready:
                        time.sleep(0.1)  # stdout closed before the process exited
                    if proc.poll() is not None:
                        emit(_drain(proc.stdout), final=True)
                        break
                    if record.get("cancelled"):
                        record["state"] = "cancelled"
                        break
                    if time.time() - started >= timeout:
                        record["state"] = "timed_out"
                        break
                if record.get("state") in ("cancelled", "timed_out"):
                    try: os.killpg(proc.pid, signal.SIGTERM)
                    except ProcessLookupError: pass
                    try: proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        try: os.killpg(proc.pid, signal.SIGKILL)
                        except ProcessLookupError: pass
                        proc.wait()
                    emit(_drain(proc.stdout), final=True)
                else:
                    record.update(state="succeeded" if proc.returncode == 0 else "failed", returncode=proc.returncode)
            finally:
                selector.close()
                if proc.stdout: proc.stdout.close()
    except Exception as exc:
        record.update(state="failed", error=str(exc))
        with log_path.open("ab") as out: out.write((str(exc) + "\n").encode())
    finally:
        record["finished"] = time.time()
        record["process"] = None
        with LOCK:
            CURRENT = None
            (JOBS / "state.json").write_text(json.dumps({"running": None}))
            logs = sorted(JOBS.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
            for old in logs[20:]: old.unlink(missing_ok=True)


def start_job(project, command, env=None, timeout=None):
    global CURRENT
    path = project_path(project)
    gradle_properties()
    with LOCK:
        if CURRENT and CURRENT.get("state") in ("queued", "running"):
            return {"error": f"Job {CURRENT['id']} is already running"}
        job_id = uuid.uuid4().hex[:12]
        merged = os.environ.copy()
        if env: merged.update(env)
        record = {"id": job_id, "state": "queued", "started": time.time(), "process": None}
        CURRENT = record
        RECORDS[job_id] = record
    thread = threading.Thread(target=_run_job, args=(job_id, command, path, merged, timeout or JOB_TIMEOUT, record), daemon=True)
    thread.start()
    return {"job_id": job_id, "state": "queued"}


def status(job_id, tail_lines=100):
    if not re.fullmatch(r"[0-9a-f]{12}", job_id): return {"error": "Invalid job id"}
    record = RECORDS.get(job_id)
    path = JOBS / f"{job_id}.log"
    if not record and not path.exists(): return {"error": "Unknown job id"}
    data = path.read_bytes() if path.exists() else b""
    lines = data.decode(errors="replace").splitlines()
    tail_lines = max(1, min(int(tail_lines), 1000))
    info = {k: v for k, v in (record or {}).items() if k != "process"}
    if not info:
        info = {"id": job_id, "state": "lost" if json.loads((JOBS / "state.json").read_text()).get("running") == job_id else "finished_unknown"}
    info["log"] = "\n".join(lines[-tail_lines:])
    info["truncated"] = len(lines) > tail_lines
    return info


def cancel(job_id):
    if not re.fullmatch(r"[0-9a-f]{12}", job_id): return {"error": "Invalid job id"}
    if not CURRENT or CURRENT.get("id") != job_id or CURRENT.get("state") not in ("queued", "running"):
        return {"error": "No running job with that id"}
    CURRENT["cancelled"] = True
    return {"job_id": job_id, "state": "cancelling"}
