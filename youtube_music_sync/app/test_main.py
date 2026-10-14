"""Orchestration: what gets planned, what gets downloaded, and what happens to
a track that only reveals itself as a duplicate after it has been fetched."""

import os

import pytest

import main
from downloader import DownloadResult, PlaylistEntry
from library import LibraryEntry, LibraryIndex, TitleVariant
from settings import Config
from state import STATUS_DUPLICATE_AFTER_DOWNLOAD, STATUS_FAILED, STATUS_OK, Store


@pytest.fixture(autouse=True)
def not_stopping():
    main._stop.clear()
    yield
    main._stop.clear()


def cfg(tmp_path, **kwargs):
    base = dict(playlists=("u",), music_dir=str(tmp_path / "music"),
                data_dir=str(tmp_path / "data"), sleep_between_downloads=0,
                dry_run=False)
    base.update(kwargs)
    config = Config(**base)
    os.makedirs(config.music_dir, exist_ok=True)
    return config


def store_for(tmp_path):
    return Store(str(tmp_path / "data" / "state.db"))


def entry(video_id="aaaaaaaaaaa", title="Some Band - Song Name", duration=215.0):
    return PlaylistEntry(video_id=video_id, title=title, duration=duration,
                         uploader="C", playlist="P")


def owned(path="/m/Some Band - Song Name.mp3", duration=215.0,
          name="Some Band - Song Name"):
    return LibraryEntry(path=path, duration=duration, video_id=None,
                        variants=(TitleVariant.build(name, "test"),))


# ------------------------------------------------------------------ utilities

def test_unique_path_sidesteps_collisions(tmp_path):
    target = tmp_path / "Track.m4a"
    assert main._unique_path(str(target)) == str(target)

    target.write_bytes(b"x")
    assert main._unique_path(str(target)) == str(tmp_path / "Track (2).m4a")

    (tmp_path / "Track (2).m4a").write_bytes(b"x")
    assert main._unique_path(str(target)) == str(tmp_path / "Track (3).m4a")


def test_dispose_duplicate_quarantines_by_default(tmp_path):
    config = cfg(tmp_path)
    downloaded = os.path.join(config.music_dir, "Track.m4a")
    with open(downloaded, "wb") as fh:
        fh.write(b"x")

    action, final = main._dispose_duplicate(config, downloaded)

    assert "moved to" in action
    assert not os.path.exists(downloaded)
    assert final and os.path.isfile(final)
    assert os.path.dirname(final) == config.quarantine_dir


def test_dispose_duplicate_delete_and_keep(tmp_path):
    deleting = cfg(tmp_path, on_duplicate="delete")
    path = os.path.join(deleting.music_dir, "Gone.m4a")
    open(path, "wb").close()
    action, final = main._dispose_duplicate(deleting, path)
    assert action == "deleted" and final is None and not os.path.exists(path)

    keeping = cfg(tmp_path, on_duplicate="keep")
    path = os.path.join(keeping.music_dir, "Stays.m4a")
    open(path, "wb").close()
    action, final = main._dispose_duplicate(keeping, path)
    assert final == path and os.path.isfile(path)


# ------------------------------------------------------------------- planning

def test_plan_skips_tracks_already_downloaded(tmp_path):
    config, store = cfg(tmp_path), store_for(tmp_path)
    store.record_download("aaaaaaaaaaa", playlist="P", title="T", uploader="C",
                          duration=215.0, path="/m/T.m4a", status=STATUS_OK)
    plan, report = _plan(config, store, LibraryIndex(), [entry()])
    assert plan == [] and report.planned == 0


def test_plan_filters_by_duration(tmp_path):
    config = cfg(tmp_path, min_duration=60, max_duration=600)
    store = store_for(tmp_path)
    entries = [entry("aaaaaaaaaaa", "Short Clip", 30.0),
               entry("bbbbbbbbbbb", "Hour Long Mix", 4000.0),
               entry("ccccccccccc", "Proper Song", 200.0)]
    plan, report = _plan(config, store, LibraryIndex(), entries)

    assert [e.video_id for e, _ in plan] == ["ccccccccccc"]
    assert report.filtered == 2
    assert store.skip_counts() == {"too_short": 1, "too_long": 1}


def test_plan_skips_a_track_the_library_already_has(tmp_path):
    config, store = cfg(tmp_path), store_for(tmp_path)
    index = LibraryIndex([owned()])
    plan, report = _plan(config, store, index,
                         [entry(title="Some Band - Song Name (Official Video)", duration=217.0)])

    assert plan == []
    assert report.duplicates == 1
    assert store.skip_counts() == {"duplicate": 1}


def test_plan_downloads_a_different_version_but_flags_it(tmp_path):
    config, store = cfg(tmp_path), store_for(tmp_path)
    index = LibraryIndex([owned()])
    plan, report = _plan(config, store, index,
                         [entry(title="Some Band - Song Name (Live)", duration=215.0)])

    # A live take is not the studio cut - it should still be fetched.
    assert len(plan) == 1
    assert report.near_misses == 1
    assert report.duplicates == 0


def test_plan_respects_the_per_cycle_cap(tmp_path):
    config = cfg(tmp_path, max_downloads_per_cycle=2)
    store = store_for(tmp_path)
    entries = [entry(f"vid{i:08d}", f"Track {i}", 200.0) for i in range(5)]
    plan, report = _plan(config, store, LibraryIndex(), entries)
    assert len(plan) == 2 and report.planned == 2


def test_plan_writes_nothing_when_not_recording(tmp_path):
    config, store = cfg(tmp_path, dry_run=True), store_for(tmp_path)
    index = LibraryIndex([owned()])
    plan, report = main._plan_cycle(
        config, store, index,
        [entry(title="Some Band - Song Name", duration=215.0)], record=False)

    assert plan == [] and report.duplicates == 1
    assert store.skip_counts() == {}


def _plan(config, store, index, entries):
    return main._plan_cycle(config, store, index, entries, record=True)


# ------------------------------------------------------------------ downloads

@pytest.fixture
def fake_metadata(monkeypatch):
    """Stand in for reading tags off the finished file."""
    def setter(duration):
        def reader(path):
            return duration, None, (TitleVariant.build(
                os.path.splitext(os.path.basename(path))[0], "filename"),)
        monkeypatch.setattr(main, "read_file_metadata", reader)
    return setter


def test_successful_download_is_recorded_and_indexed(tmp_path, monkeypatch, fake_metadata):
    config, store = cfg(tmp_path), store_for(tmp_path)
    index = LibraryIndex()
    saved = os.path.join(config.music_dir, "Some Band - Song Name.m4a")
    open(saved, "wb").close()

    monkeypatch.setattr(main.downloader, "download",
                        lambda c, e, **kw: DownloadResult(ok=True, path=saved))
    monkeypatch.setattr(main.downloader, "stamp_video_id", lambda p, v: True)
    fake_metadata(215.0)

    report = main.CycleReport()
    main._run_downloads(config, store, index, [(entry(), None)], report)

    assert report.downloaded == 1 and report.failures == 0
    assert store.downloaded_ids() == {"aaaaaaaaaaa"}
    # Indexed live, so the same track on a second playlist is not fetched twice.
    assert index.match("Some Band - Song Name", 215.0).is_duplicate


def test_duplicate_found_after_download_is_quarantined(tmp_path, monkeypatch, fake_metadata):
    config, store = cfg(tmp_path), store_for(tmp_path)
    index = LibraryIndex([owned()])
    saved = os.path.join(config.music_dir, "Some Band - Song Name.m4a")
    open(saved, "wb").close()

    monkeypatch.setattr(main.downloader, "download",
                        lambda c, e, **kw: DownloadResult(ok=True, path=saved))
    monkeypatch.setattr(main.downloader, "stamp_video_id", lambda p, v: True)
    # The playlist listing had no duration; the real file turns out to match.
    fake_metadata(215.0)

    report = main.CycleReport()
    main._run_downloads(config, store, index,
                        [(entry(title="Some Band - Song Name", duration=None), None)], report)

    assert report.duplicates_after_download == 1 and report.downloaded == 0
    assert not os.path.exists(saved)
    assert os.path.isfile(os.path.join(config.quarantine_dir, "Some Band - Song Name.m4a"))
    assert store.skip_counts() == {"duplicate_after_download": 1}
    assert store.stats()["downloaded"] == 0
    # Handled, so it is never fetched again.
    assert store.downloaded_ids() == {"aaaaaaaaaaa"}


def test_failed_download_is_recorded_for_retry(tmp_path, monkeypatch):
    config, store = cfg(tmp_path), store_for(tmp_path)
    monkeypatch.setattr(main.downloader, "download",
                        lambda c, e, **kw: DownloadResult(ok=False, error="ERROR: blocked"))

    report = main.CycleReport()
    main._run_downloads(config, store, LibraryIndex(), [(entry(), None)], report)

    assert report.failures == 1 and report.downloaded == 0
    assert store.stats()["failed"] == 1
    assert store.downloaded_ids() == set()


def test_name_collision_is_reported_not_counted_as_a_download(tmp_path, monkeypatch):
    config, store = cfg(tmp_path), store_for(tmp_path)
    monkeypatch.setattr(
        main.downloader, "download",
        lambda c, e, **kw: DownloadResult(ok=False, skipped_existing=True,
                                          error="a file of that name already exists"))

    report = main.CycleReport()
    main._run_downloads(config, store, LibraryIndex(), [(entry(), None)], report)

    assert report.collisions == 1 and report.downloaded == 0 and report.failures == 0
    assert store.skip_counts() == {"name_collision": 1}


def test_a_crashing_downloader_does_not_abort_the_cycle(tmp_path, monkeypatch):
    config, store = cfg(tmp_path), store_for(tmp_path)

    def exploding(c, e, **kw):
        raise OSError("subprocess died")
    monkeypatch.setattr(main.downloader, "download", exploding)

    report = main.CycleReport()
    main._run_downloads(config, store, LibraryIndex(),
                        [(entry(), None), (entry("bbbbbbbbbbb"), None)], report)

    assert report.failures == 2
    assert store.stats()["failed"] == 2


def test_stop_signal_halts_between_downloads(tmp_path, monkeypatch, fake_metadata):
    config, store = cfg(tmp_path), store_for(tmp_path)
    calls = []

    def counting(c, e, **kw):
        calls.append(e.video_id)
        main._stop.set()
        saved = os.path.join(config.music_dir, f"{e.video_id}.m4a")
        open(saved, "wb").close()
        return DownloadResult(ok=True, path=saved)

    monkeypatch.setattr(main.downloader, "download", counting)
    monkeypatch.setattr(main.downloader, "stamp_video_id", lambda p, v: True)
    fake_metadata(200.0)

    report = main.CycleReport()
    main._run_downloads(config, store, LibraryIndex(),
                        [(entry("aaaaaaaaaaa"), None), (entry("bbbbbbbbbbb"), None)], report)

    assert calls == ["aaaaaaaaaaa"]
    assert report.downloaded == 1


def test_report_problem_flag():
    assert not main.CycleReport().problem
    assert main.CycleReport(failures=1).problem
    assert main.CycleReport(playlist_errors=1).problem
    assert not main.CycleReport(duplicates=5, near_misses=2).problem


# ------------------------------------------- unavailable videos and the budget

def test_an_unavailable_video_is_dropped_from_later_plans(tmp_path):
    config, store = cfg(tmp_path), store_for(tmp_path)
    store.record_failure("aaaaaaaaaaa", playlist="P", title="T", uploader="C",
                         duration=None, permanent=True,
                         error="ERROR: [youtube] aaaaaaaaaaa: Video unavailable",
                         cap_days=config.recheck_unavailable_days)
    plan, report = _plan(config, store, LibraryIndex(), [entry()])
    assert plan == []
    assert report.deferred == 1
    assert report.planned == 0


def test_a_transiently_failed_video_is_still_planned(tmp_path):
    config, store = cfg(tmp_path), store_for(tmp_path)
    store.record_failure("aaaaaaaaaaa", playlist="P", title="T", uploader="C",
                         duration=None, permanent=False,
                         error="ERROR: aria2c exited with code 22",
                         cap_days=config.recheck_unavailable_days)
    plan, report = _plan(config, store, LibraryIndex(), [entry()])
    assert len(plan) == 1
    assert report.deferred == 0


def test_dead_entries_do_not_consume_the_per_cycle_budget(tmp_path):
    """The actual complaint: dead videos were eating the download slots."""
    config = cfg(tmp_path, max_downloads_per_cycle=3)
    store = store_for(tmp_path)
    dead = [entry(video_id=f"dead{i:07d}", title=f"Dead {i}") for i in range(5)]
    live = [entry(video_id=f"live{i:07d}", title=f"Live Song {i}") for i in range(3)]
    for e in dead:
        store.record_failure(e.video_id, playlist="P", title=e.title, uploader="C",
                             duration=None, permanent=True,
                             error="ERROR: [youtube] x: Private video",
                             cap_days=config.recheck_unavailable_days)

    plan, report = _plan(config, store, LibraryIndex(), dead + live)

    # All three slots go to downloadable tracks, not to the dead ones that
    # happen to come first in the playlist.
    assert len(plan) == 3
    assert [e.video_id for e, _ in plan] == [e.video_id for e in live]
    assert report.deferred == 5


def test_a_retired_video_is_never_planned_again(tmp_path):
    config = cfg(tmp_path, recheck_unavailable_days=0)
    store = store_for(tmp_path)
    store.record_failure("aaaaaaaaaaa", playlist="P", title="T", uploader="C",
                         duration=None, permanent=True, error="Video unavailable",
                         cap_days=0)
    plan, report = _plan(config, store, LibraryIndex(), [entry()])
    assert plan == [] and report.deferred == 1


def test_an_unavailable_download_backs_itself_off(tmp_path, monkeypatch):
    """End to end: the loop records the failure and the next plan skips it."""
    config, store = cfg(tmp_path), store_for(tmp_path)
    monkeypatch.setattr(main.downloader, "download", lambda c, e, **kw: DownloadResult(
        ok=False, error="ERROR: [youtube] aaaaaaaaaaa: Video unavailable"))
    index = LibraryIndex()

    plan, report = _plan(config, store, index, [entry()])
    assert len(plan) == 1
    main._run_downloads(config, store, index, plan, report)
    assert report.failures == 1

    # Same playlist, next cycle: now skipped rather than retried.
    plan2, report2 = _plan(config, store, index, [entry()])
    assert plan2 == [] and report2.deferred == 1


def test_a_transient_download_failure_is_retried_next_cycle(tmp_path, monkeypatch):
    config, store = cfg(tmp_path), store_for(tmp_path)
    monkeypatch.setattr(main.downloader, "download", lambda c, e, **kw: DownloadResult(
        ok=False, error="ERROR: aria2c exited with code 22"))
    index = LibraryIndex()

    plan, report = _plan(config, store, index, [entry()])
    main._run_downloads(config, store, index, plan, report)
    assert report.failures == 1

    plan2, report2 = _plan(config, store, index, [entry()])
    assert len(plan2) == 1 and report2.deferred == 0


# ------------------------------------------------------------------- progress

def test_progress_is_throttled_not_logged_every_frame(tmp_path):
    config = cfg(tmp_path, progress_interval_seconds=30)
    report = main._progress_logger(config, "Long Mix")
    logged = []
    import logging as _logging

    class Capture(_logging.Handler):
        def emit(self, record):
            logged.append(record.getMessage())

    handler = Capture()
    previous = main._LOG.level
    main._LOG.addHandler(handler)
    main._LOG.setLevel(_logging.INFO)
    try:
        for _ in range(200):
            report("12.3% of 200MiB at 1MiB/s ETA 02:30")
        report("100.0% of 200MiB at 1MiB/s ETA 00:00")
    finally:
        main._LOG.removeHandler(handler)
        main._LOG.setLevel(previous)

    # One line for the burst, plus the completion line - not 201.
    assert len(logged) == 2
    assert "Long Mix" in logged[0]
    assert "100.0%" in logged[-1]


def test_progress_can_be_turned_off(tmp_path):
    assert main._progress_logger(cfg(tmp_path, progress_interval_seconds=0), "T") is None


def test_the_download_timeout_comes_from_the_options(tmp_path, monkeypatch):
    config = cfg(tmp_path, download_timeout_minutes=180)
    seen = {}

    def fake_stream(argv, timeout, on_progress=None):
        seen["timeout"] = timeout
        target = argv[argv.index("--print-to-file") + 2]
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(str(tmp_path / "out.opus") + "\n")
        return 0, []

    monkeypatch.setattr(main.downloader, "_run_streaming", fake_stream)
    main.downloader.download(config, entry())
    # Three hours of options, not the old fixed thirty minutes.
    assert seen["timeout"] == 180 * 60
