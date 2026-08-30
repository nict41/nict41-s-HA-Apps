"""Tests for the download scheduler: how many transfers run, and when.

`_launch` is stubbed throughout so a tick makes scheduling decisions without
any transfer actually starting.
"""

from datetime import datetime

import pytest

import downloads
import settings

URL = "https://lb.download.kiwix.org/zim/wikipedia/{}"


@pytest.fixture(autouse=True)
def _no_background(monkeypatch):
    monkeypatch.setattr(downloads, "start_scheduler", lambda: None)


@pytest.fixture
def launched(monkeypatch):
    """Records what the scheduler chose to start."""
    started = []

    def fake_launch(job_id):
        started.append(job_id)
        downloads._update(job_id, status="downloading")

    monkeypatch.setattr(downloads, "_launch", fake_launch)
    return started


def _queue(zim_dir, name):
    job, error = downloads.start(URL.format(name), name, name)
    assert error == "", error
    return job["id"]


def _at(hour):
    return datetime(2026, 8, 30, hour, 0)


def test_the_scheduler_fills_up_to_the_limit(zim_dir, launched):
    settings.set_many({"max_concurrent_downloads": 2})
    for name in ("a.zim", "b.zim", "c.zim"):
        _queue(zim_dir, name)

    downloads.tick()

    assert len(launched) == 2
    assert downloads.active_count() == 2


def test_raising_the_limit_starts_more_without_a_restart(zim_dir, launched):
    settings.set_many({"max_concurrent_downloads": 1})
    for name in ("a.zim", "b.zim", "c.zim"):
        _queue(zim_dir, name)
    downloads.tick()
    assert len(launched) == 1

    settings.set_many({"max_concurrent_downloads": 3})
    downloads.tick()

    assert len(launched) == 3


def test_lowering_the_limit_never_kills_a_running_transfer(zim_dir, launched):
    settings.set_many({"max_concurrent_downloads": 3})
    for name in ("a.zim", "b.zim", "c.zim"):
        _queue(zim_dir, name)
    downloads.tick()

    settings.set_many({"max_concurrent_downloads": 1})
    downloads.tick()

    # The extra transfers are left to finish; the new limit applies to what
    # starts next, because stopping a running download to obey a slider
    # would throw away the connection for nothing.
    assert all(job["status"] == "downloading" for job in downloads.snapshot())


def test_downloads_are_started_oldest_first(zim_dir, launched):
    settings.set_many({"max_concurrent_downloads": 1})
    first = _queue(zim_dir, "a.zim")
    downloads._update(first, created_at=1.0)
    second = _queue(zim_dir, "b.zim")
    downloads._update(second, created_at=2.0)

    downloads.tick()

    assert launched == [first]


# ── The off-peak window ─────────────────────────────────────────────────────

def test_nothing_starts_outside_the_window(zim_dir, launched):
    settings.set_many({"window_enabled": True, "window_start": "23:00", "window_end": "07:00"})
    job_id = _queue(zim_dir, "a.zim")

    downloads.tick(_at(13))

    assert launched == []
    assert downloads.get(job_id)["status"] == "waiting"


def test_the_window_opening_starts_the_waiting_downloads(zim_dir, launched):
    settings.set_many({"window_enabled": True, "window_start": "23:00", "window_end": "07:00",
                       "max_concurrent_downloads": 2})
    _queue(zim_dir, "a.zim")
    _queue(zim_dir, "b.zim")
    downloads.tick(_at(13))
    assert launched == []

    downloads.tick(_at(23))

    assert len(launched) == 2


def test_a_running_transfer_stands_down_when_the_window_closes(zim_dir, launched):
    settings.set_many({"window_enabled": True, "window_start": "23:00", "window_end": "07:00"})
    job_id = _queue(zim_dir, "a.zim")
    downloads.tick(_at(23))
    assert launched == [job_id]

    downloads.tick(_at(8))

    # The transfer loop sees this and stops after its current chunk, keeping
    # the partial file; the job then shows as waiting rather than paused.
    with downloads._lock:
        assert downloads._jobs[job_id]["stop"] == "waiting"


def test_download_now_ignores_the_window(zim_dir, launched):
    settings.set_many({"window_enabled": True, "window_start": "23:00", "window_end": "07:00"})
    job_id = _queue(zim_dir, "a.zim")
    downloads.tick(_at(13))
    assert downloads.get(job_id)["status"] == "waiting"

    downloads.download_now(job_id)
    downloads.tick(_at(13))

    assert launched == [job_id]


def test_a_job_told_to_download_now_is_not_paused_by_a_later_tick(zim_dir, launched):
    settings.set_many({"window_enabled": True, "window_start": "23:00", "window_end": "07:00"})
    job_id = _queue(zim_dir, "a.zim")
    downloads.download_now(job_id)
    downloads.tick(_at(13))

    downloads.tick(_at(14))

    with downloads._lock:
        assert "stop" not in downloads._jobs[job_id]


def test_transfer_rate_is_sampled_between_ticks(zim_dir, launched):
    job_id = _queue(zim_dir, "a.zim")
    downloads.tick()
    downloads._update(job_id, downloaded=10_000_000)

    downloads.tick()

    assert downloads.get(job_id)["speed"] > 0


def test_a_paused_job_is_not_restarted_by_the_scheduler(zim_dir, launched):
    job_id = _queue(zim_dir, "a.zim")
    downloads.tick()
    launched.clear()
    downloads._update(job_id, status="paused")

    downloads.tick()

    assert launched == []
    assert downloads.get(job_id)["status"] == "paused"
