from pathlib import Path

import httpx
import pytest

import downloads
import library
import settings

URL = "https://lb.download.kiwix.org/zim/wikipedia/wikipedia_en_mini.zim"
FILENAME = "wikipedia_en_mini.zim"
BODY = b"ZIM-DATA-" * 32


class _FakeStream:
    """Stands in for httpx.stream(): records the request, replays a body."""

    def __init__(self, chunks, status=200, headers=None, raises=None):
        self.chunks = chunks
        self.status_code = status
        self.headers = httpx.Headers(headers or {"content-length": str(sum(map(len, chunks)))})
        self.raises = raises
        self.request_headers = None

    def __call__(self, method, url, headers=None, **kwargs):
        self.request_headers = headers or {}
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def raise_for_status(self):
        if self.raises:
            raise self.raises

    def iter_bytes(self, chunk_size=None):
        yield from self.chunks


@pytest.fixture(autouse=True)
def _no_workers(monkeypatch):
    """Run transfers on the calling thread so tests stay deterministic."""
    monkeypatch.setattr(downloads, "_ensure_workers", lambda: None)


def _queued(**overrides):
    job, error = downloads.start(
        overrides.get("url", URL),
        overrides.get("filename", FILENAME),
        "Wikipedia mini",
        overrides.get("size"),
    )
    assert error == "", error
    return job["id"]


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


def test_a_completed_download_is_published_and_served(zim_dir, monkeypatch):
    monkeypatch.setattr(downloads.httpx, "stream", _FakeStream([BODY]))
    monkeypatch.setattr(settings, "AUTO_SERVE_NEW", True)
    job_id = _queued(size=len(BODY))

    downloads._run(job_id)

    assert (Path(zim_dir) / FILENAME).read_bytes() == BODY
    assert not (Path(zim_dir) / (FILENAME + ".part")).exists()
    assert downloads.get(job_id)["status"] == "done"
    assert library.read_state()["served"] == [FILENAME]


def test_auto_serve_off_leaves_the_selection_alone(zim_dir, monkeypatch):
    monkeypatch.setattr(downloads.httpx, "stream", _FakeStream([BODY]))
    monkeypatch.setattr(settings, "AUTO_SERVE_NEW", False)

    downloads._run(_queued(size=len(BODY)))

    assert library.read_state()["served"] == []


def test_an_interrupted_download_resumes_with_a_range_request(zim_dir, monkeypatch):
    partial = Path(zim_dir) / (FILENAME + ".part")
    partial.write_bytes(BODY[:100])
    stream = _FakeStream(
        [BODY[100:]],
        status=206,
        headers={"content-range": f"bytes 100-{len(BODY) - 1}/{len(BODY)}"},
    )
    monkeypatch.setattr(downloads.httpx, "stream", stream)

    downloads._run(_queued())

    assert stream.request_headers["Range"] == "bytes=100-"
    assert (Path(zim_dir) / FILENAME).read_bytes() == BODY


def test_a_mirror_that_ignores_range_restarts_from_the_beginning(zim_dir, monkeypatch):
    (Path(zim_dir) / (FILENAME + ".part")).write_bytes(b"STALE")
    monkeypatch.setattr(downloads.httpx, "stream", _FakeStream([BODY], status=200))

    downloads._run(_queued())

    assert (Path(zim_dir) / FILENAME).read_bytes() == BODY


def test_an_already_complete_partial_is_just_finalised(zim_dir, monkeypatch):
    (Path(zim_dir) / (FILENAME + ".part")).write_bytes(BODY)
    monkeypatch.setattr(downloads.httpx, "stream", _FakeStream([], status=416))

    downloads._run(_queued())

    assert (Path(zim_dir) / FILENAME).read_bytes() == BODY
    assert downloads.get(_only_job()["id"])["status"] == "done"


def test_the_share_disappearing_mid_transfer_keeps_the_partial_and_explains(zim_dir, monkeypatch):
    class _Failing(_FakeStream):
        def iter_bytes(self, chunk_size=None):
            yield BODY[:10]
            raise OSError("Stale file handle")

    monkeypatch.setattr(downloads.httpx, "stream", _Failing([BODY]))
    job_id = _queued()

    downloads._run(job_id)

    job = downloads.get(job_id)
    assert job["status"] == "error"
    assert "share" in job["error"] and "resume" in job["error"]
    assert (Path(zim_dir) / (FILENAME + ".part")).exists()


def test_a_network_failure_is_reported_without_killing_the_job(zim_dir, monkeypatch):
    monkeypatch.setattr(
        downloads.httpx, "stream", _FakeStream([], raises=httpx.ConnectError("dns"))
    )
    job_id = _queued()

    downloads._run(job_id)

    assert downloads.get(job_id)["status"] == "error"
    assert "Download failed" in downloads.get(job_id)["error"]


def test_cancelling_stops_the_transfer_and_keeps_the_partial(zim_dir, monkeypatch):
    class _Cancelling(_FakeStream):
        def iter_bytes(self, chunk_size=None):
            yield BODY[:20]
            downloads.cancel(_only_job()["id"])
            yield BODY[20:]

    monkeypatch.setattr(downloads.httpx, "stream", _Cancelling([BODY]))
    job_id = _queued()

    downloads._run(job_id)

    assert downloads.get(job_id)["status"] == "paused"
    assert (Path(zim_dir) / (FILENAME + ".part")).read_bytes() == BODY[:20]
    assert not (Path(zim_dir) / FILENAME).exists()


def test_restarting_a_finished_job_supersedes_it_rather_than_duplicating(zim_dir, monkeypatch):
    monkeypatch.setattr(downloads.httpx, "stream", _FakeStream([BODY]))
    downloads._run(_queued())
    (Path(zim_dir) / FILENAME).unlink()

    _queued()

    assert len(downloads.snapshot()) == 1


def test_partial_files_are_relisted_as_interrupted_after_a_restart(zim_dir):
    (Path(zim_dir) / (FILENAME + ".part")).write_bytes(BODY[:5])

    downloads.adopt_partials()

    job = _only_job()
    assert job["status"] == "interrupted"
    assert job["downloaded"] == 5


def _only_job():
    jobs = downloads.snapshot()
    assert len(jobs) == 1, jobs
    return jobs[0]
