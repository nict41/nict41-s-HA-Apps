"""Tests for the downloader.

The fake below behaves like a real mirror: it honours Range requests, so
resuming and segmenting are exercised for real rather than being asserted
about. `_run()` is called directly on the calling thread (it joins its own
segment threads) so every test stays deterministic.
"""

import json
from pathlib import Path

import httpx
import pytest

import catalog
import downloads
import library
import settings

URL = "https://lb.download.kiwix.org/zim/wikipedia/wikipedia_en_mini.zim"
FILENAME = "wikipedia_en_mini.zim"
BODY = bytes(range(256)) * 512  # 128 KB of non-repeating-enough data


class FakeMirror:
    """Serves BODY over fake httpx.stream/head calls, honouring Range."""

    def __init__(self, body=BODY, ranges=True, chunk=4096):
        self.body = body
        self.ranges = ranges
        self.chunk = chunk
        self.requests = []
        self.fail_after = None

    def head(self, url, **kwargs):
        headers = {"content-length": str(len(self.body))}
        if self.ranges:
            headers["accept-ranges"] = "bytes"
        return httpx.Response(200, headers=headers, request=httpx.Request("HEAD", url))

    def stream(self, method, url, headers=None, **kwargs):
        headers = headers or {}
        self.requests.append(headers.get("Range", ""))
        start, end = 0, len(self.body) - 1
        ranged = "Range" in headers and self.ranges
        if ranged:
            spec = headers["Range"].split("=", 1)[1]
            first, _, last = spec.partition("-")
            start = int(first)
            end = int(last) if last else len(self.body) - 1
            if start >= len(self.body):
                return _Stream(416, b"", {}, self)
        payload = self.body[start:end + 1]
        response_headers = {"content-length": str(len(payload))}
        if ranged:
            response_headers["content-range"] = f"bytes {start}-{end}/{len(self.body)}"
        return _Stream(206 if ranged else 200, payload, response_headers, self)


class _Stream:
    def __init__(self, status, payload, headers, mirror):
        self.status_code = status
        self.headers = httpx.Headers(headers)
        self._payload = payload
        self._mirror = mirror

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)

    def iter_bytes(self, chunk_size=None):
        # A real connection hands over whatever has arrived, not the size the
        # caller asked for, so the mirror's own chunking wins here.
        size = self._mirror.chunk
        for offset in range(0, len(self._payload), size):
            yield self._payload[offset:offset + size]


@pytest.fixture(autouse=True)
def _no_scheduler(monkeypatch):
    """Tests drive the scheduler themselves; nothing runs in the background."""
    monkeypatch.setattr(downloads, "start_scheduler", lambda: None)


@pytest.fixture
def mirror(monkeypatch):
    fake = FakeMirror()
    monkeypatch.setattr(downloads.httpx, "stream", fake.stream)
    monkeypatch.setattr(downloads.httpx, "head", fake.head)
    return fake


def _queued(**overrides):
    job, error = downloads.start(
        overrides.get("url", URL),
        overrides.get("filename", FILENAME),
        "Wikipedia mini",
        overrides.get("size"),
    )
    assert error == "", error
    return job["id"]


def _part(zim_dir, suffix=".part"):
    return Path(zim_dir) / (FILENAME + suffix)


# ── Refusals ────────────────────────────────────────────────────────────────

def test_download_is_refused_when_the_share_is_unavailable(monkeypatch):
    monkeypatch.setenv("ZIM_PATH", "GhostNAS/Kiwix")
    job, error = downloads.start(URL, FILENAME)
    assert job is None
    assert "isn't mounted" in error


def test_non_zim_and_non_http_targets_are_refused(zim_dir):
    assert downloads.start("file:///etc/passwd", FILENAME)[1]
    assert downloads.start(URL, "../escape.zim")[1]
    assert downloads.start(URL, "notes.txt")[1]


def test_an_archive_already_on_the_share_is_not_downloaded_again(zim_dir):
    (Path(zim_dir) / FILENAME).write_bytes(b"x")
    job, error = downloads.start(URL, FILENAME)
    assert job is None
    assert "already in your library" in error


def test_an_archive_too_big_for_the_share_is_refused_before_it_starts(zim_dir, monkeypatch):
    status = dict(settings.storage_status())
    status["free_bytes"] = 1_000_000
    monkeypatch.setattr(settings, "storage_status", lambda: status)

    job, error = downloads.start(URL, FILENAME, size=900_000_000_000)

    assert job is None
    assert "Not enough space" in error


# ── Single-connection transfers ─────────────────────────────────────────────

def test_a_completed_download_is_published_and_served(zim_dir, mirror):
    settings.set_many({"download_threads": 1, "auto_serve_new": True})

    downloads._run(_queued(size=len(BODY)))

    assert (Path(zim_dir) / FILENAME).read_bytes() == BODY
    assert not _part(zim_dir).exists()
    assert library.read_state()["served"] == [FILENAME]


def test_auto_serve_off_leaves_the_selection_alone(zim_dir, mirror):
    settings.set_many({"download_threads": 1, "auto_serve_new": False})

    downloads._run(_queued(size=len(BODY)))

    assert library.read_state()["served"] == []


def test_a_mirror_that_ignores_range_restarts_from_the_beginning(zim_dir, monkeypatch):
    settings.set_many({"download_threads": 1})
    fake = FakeMirror(ranges=False)
    monkeypatch.setattr(downloads.httpx, "stream", fake.stream)
    monkeypatch.setattr(downloads.httpx, "head", fake.head)
    _part(zim_dir).write_bytes(b"STALE")

    downloads._run(_queued())

    assert (Path(zim_dir) / FILENAME).read_bytes() == BODY


def test_an_already_complete_partial_is_just_finalised(zim_dir, mirror):
    settings.set_many({"download_threads": 1})
    _part(zim_dir).write_bytes(BODY)

    job_id = _queued()
    downloads._run(job_id)

    assert (Path(zim_dir) / FILENAME).read_bytes() == BODY
    assert downloads.get(job_id)["status"] == "done"


def test_the_share_disappearing_mid_transfer_keeps_the_partial_and_explains(zim_dir, monkeypatch):
    settings.set_many({"download_threads": 1})

    class Failing(FakeMirror):
        def stream(self, method, url, headers=None, **kwargs):
            stream = super().stream(method, url, headers, **kwargs)
            original = stream.iter_bytes

            def blow_up(chunk_size=None):
                yield next(iter(original(chunk_size)))
                raise OSError("Stale file handle")

            stream.iter_bytes = blow_up
            return stream

    fake = Failing()
    monkeypatch.setattr(downloads.httpx, "stream", fake.stream)
    monkeypatch.setattr(downloads.httpx, "head", fake.head)

    job_id = _queued()
    downloads._run(job_id)

    job = downloads.get(job_id)
    assert job["status"] == "error"
    assert "share" in job["error"] and "resume" in job["error"]
    assert _part(zim_dir).exists()


# ── Compatibility with downloads started by earlier versions ────────────────

def test_a_plain_part_file_resumes_from_where_it_stopped(zim_dir, mirror):
    """The shape every pre-segment download leaves behind: a contiguous
    prefix and no sidecar. It must resume, not restart."""
    settings.set_many({"download_threads": 1})
    _part(zim_dir).write_bytes(BODY[:5000])

    downloads._run(_queued())

    assert (Path(zim_dir) / FILENAME).read_bytes() == BODY
    assert mirror.requests[-1] == "bytes=5000-"


def test_a_plain_part_file_converts_to_segments_without_refetching(zim_dir, mirror, monkeypatch):
    monkeypatch.setattr(downloads, "MIN_SEGMENT", 8 * 1024)
    settings.set_many({"download_threads": 4})
    prefix = 20_000
    _part(zim_dir).write_bytes(BODY[:prefix])

    downloads._run(_queued())

    assert (Path(zim_dir) / FILENAME).read_bytes() == BODY
    # No request may start before the bytes already on disk.
    starts = [int(r.split("=")[1].split("-")[0]) for r in mirror.requests if r]
    assert min(starts) == prefix


# ── Segmented transfers ─────────────────────────────────────────────────────

def test_a_segmented_download_assembles_the_file_correctly(zim_dir, mirror, monkeypatch):
    monkeypatch.setattr(downloads, "MIN_SEGMENT", 8 * 1024)
    settings.set_many({"download_threads": 4})

    job_id = _queued(size=len(BODY))
    downloads._run(job_id)

    assert (Path(zim_dir) / FILENAME).read_bytes() == BODY
    assert len([r for r in mirror.requests if r.startswith("bytes=")]) >= 4
    assert downloads.get(job_id)["status"] == "done"


def test_a_finished_segmented_download_leaves_no_sidecar_behind(zim_dir, mirror, monkeypatch):
    monkeypatch.setattr(downloads, "MIN_SEGMENT", 8 * 1024)
    settings.set_many({"download_threads": 4})

    downloads._run(_queued(size=len(BODY)))

    assert not _part(zim_dir, ".part.json").exists()
    assert not _part(zim_dir).exists()


def test_a_segmented_download_resumes_from_its_sidecar(zim_dir, mirror, monkeypatch):
    monkeypatch.setattr(downloads, "MIN_SEGMENT", 8 * 1024)
    settings.set_many({"download_threads": 2})

    # Half of segment one and none of segment two, as an interrupted
    # segmented transfer would leave it.
    half = len(BODY) // 2
    part = _part(zim_dir)
    part.write_bytes(b"\0" * len(BODY))
    with part.open("r+b") as handle:
        handle.write(BODY[:1000])
    _part(zim_dir, ".part.json").write_text(json.dumps({
        "version": 1, "mode": "segmented", "url": URL, "total": len(BODY),
        "segments": [
            {"start": 0, "end": half, "done": 1000},
            {"start": half, "end": len(BODY), "done": 0},
        ],
    }))

    downloads._run(_queued())

    assert (Path(zim_dir) / FILENAME).read_bytes() == BODY
    assert "bytes=1000-" + str(half - 1) in mirror.requests


def test_raising_the_thread_count_splits_what_is_left(zim_dir, monkeypatch):
    monkeypatch.setattr(downloads, "MIN_SEGMENT", 1000)
    segments = [{"start": 0, "end": 10_000, "done": 2_000}]

    resplit = downloads._resplit(segments, 4)

    pending = [s for s in resplit if s["done"] < s["end"] - s["start"]]
    assert len(pending) > 1
    # Nothing already downloaded is ever inside a range that gets refetched.
    assert min(s["start"] + s["done"] for s in pending) == 2_000
    assert resplit[-1]["end"] == 10_000


def test_segments_never_overlap_or_leave_gaps(monkeypatch):
    monkeypatch.setattr(downloads, "MIN_SEGMENT", 1000)
    segments = downloads._plan_segments(base=5_000, total=100_000, threads=5)

    assert segments[0] == {"start": 0, "end": 5_000, "done": 5_000}
    for earlier, later in zip(segments, segments[1:]):
        assert earlier["end"] == later["start"]
    assert segments[-1]["end"] == 100_000


def test_a_small_download_is_not_split(zim_dir, mirror):
    settings.set_many({"download_threads": 8})

    downloads._run(_queued(size=len(BODY)))

    assert not _part(zim_dir, ".part.json").exists()
    assert (Path(zim_dir) / FILENAME).read_bytes() == BODY


# ── Pausing, persistence and adoption ───────────────────────────────────────

def test_pausing_stops_the_transfer_and_keeps_the_partial(zim_dir, monkeypatch):
    settings.set_many({"download_threads": 1})

    class Interrupting(FakeMirror):
        def stream(self, method, url, headers=None, **kwargs):
            stream = super().stream(method, url, headers, **kwargs)
            original = stream.iter_bytes

            def pause_midway(chunk_size=None):
                for index, chunk in enumerate(original(chunk_size)):
                    if index == 1:
                        downloads.pause(downloads.snapshot()[0]["id"])
                    yield chunk

            stream.iter_bytes = pause_midway
            return stream

    fake = Interrupting()
    monkeypatch.setattr(downloads.httpx, "stream", fake.stream)
    monkeypatch.setattr(downloads.httpx, "head", fake.head)

    job_id = _queued()
    downloads._run(job_id)

    assert downloads.get(job_id)["status"] == "paused"
    assert 0 < _part(zim_dir).stat().st_size < len(BODY)
    assert not (Path(zim_dir) / FILENAME).exists()


def test_jobs_are_saved_and_come_back_ready_to_resume(zim_dir):
    job_id = _queued(size=len(BODY))
    downloads._update(job_id, status="downloading")
    _part(zim_dir).write_bytes(BODY[:4321])

    with downloads._lock:
        downloads._jobs.clear()
    downloads.load_registry()

    job = downloads.get(job_id)
    # Anything mid-flight when the add-on stopped is queued again, so the
    # scheduler restarts it by itself rather than waiting to be told.
    assert job["status"] == "queued"
    assert job["url"] == URL
    # And its position comes from the bytes on the share, not from a count
    # that was last written some megabytes ago.
    assert job["downloaded"] == 4321


def test_a_restored_segmented_job_reports_its_real_position(zim_dir):
    job_id = _queued(size=len(BODY))
    _part(zim_dir).write_bytes(b"\0" * len(BODY))
    _part(zim_dir, ".part.json").write_text(json.dumps({
        "version": 1, "mode": "segmented", "url": URL, "total": len(BODY),
        "segments": [{"start": 0, "end": 1000, "done": 700},
                     {"start": 1000, "end": len(BODY), "done": 300}],
    }))

    with downloads._lock:
        downloads._jobs.clear()
    downloads.load_registry()

    # The sparse .part is already full-length on disk; only the plan knows
    # how much of it is real.
    assert downloads.get(job_id)["downloaded"] == 1000


def test_partial_files_from_before_the_job_list_are_adopted(zim_dir):
    _part(zim_dir).write_bytes(BODY[:5])

    downloads.adopt_partials()

    job = downloads.snapshot()[0]
    assert job["status"] == "interrupted"
    assert job["downloaded"] == 5
    assert job["url"] == ""


def test_an_adopted_partial_finds_its_source_in_the_catalog(zim_dir, monkeypatch):
    _part(zim_dir).write_bytes(BODY[:5])
    downloads.adopt_partials()
    job_id = downloads.snapshot()[0]["id"]
    monkeypatch.setattr(catalog, "find_by_filename",
                        lambda name: {"url": URL, "size": len(BODY), "title": "Wikipedia mini"})

    assert downloads._resolve_url(job_id) is True
    assert downloads.get(job_id)["url"] == URL


def test_an_archive_no_longer_in_the_catalog_says_so(zim_dir, monkeypatch):
    _part(zim_dir).write_bytes(BODY[:5])
    downloads.adopt_partials()
    job_id = downloads.snapshot()[0]["id"]
    monkeypatch.setattr(catalog, "find_by_filename", lambda name: None)

    assert downloads._resolve_url(job_id) is False
    assert "isn't in the catalog any more" in downloads.get(job_id)["error"]


def test_restarting_a_finished_job_supersedes_it_rather_than_duplicating(zim_dir, mirror):
    settings.set_many({"download_threads": 1})
    downloads._run(_queued())
    (Path(zim_dir) / FILENAME).unlink()

    _queued()

    assert len(downloads.snapshot()) == 1


def test_progress_is_checkpointed_often_enough_to_survive_a_hard_kill(zim_dir, mirror, monkeypatch):
    """A killed container loses whatever the plan hasn't recorded, so the
    checkpoint has to be bounded by bytes and not only by elapsed time."""
    monkeypatch.setattr(downloads, "MIN_SEGMENT", 8 * 1024)
    monkeypatch.setattr(downloads, "PLAN_SAVE_BYTES", 8 * 1024)
    monkeypatch.setattr(downloads, "PLAN_SAVE_SECONDS", 3600.0)  # time can't be what saves us
    settings.set_many({"download_threads": 2})

    saves = []
    original = downloads._write_plan
    monkeypatch.setattr(downloads, "_write_plan", lambda path, plan: (
        saves.append(sum(s["done"] for s in plan["segments"])), original(path, plan))[1])

    downloads._run(_queued(size=len(BODY)))

    # Several checkpoints along the way, not just one at the end.
    assert len([total for total in saves if 0 < total < len(BODY)]) >= 2
