import state
from state import STATUS_DUPLICATE_AFTER_DOWNLOAD, STATUS_FAILED, STATUS_OK, Store


def make_store(tmp_path):
    return Store(str(tmp_path / "sub" / "state.db"))


def test_schema_is_created_in_a_missing_directory(tmp_path):
    store = make_store(tmp_path)
    assert store.stats() == {"downloaded": 0, "failed": 0, "skipped": 0, "library_files": 0}


def test_download_records_are_upserted_not_duplicated(tmp_path):
    store = make_store(tmp_path)
    for _ in range(3):
        store.record_download("vid00000001", playlist="p", title="T", uploader="U",
                              duration=200.0, path="/m/T.m4a", status=STATUS_OK)
    assert store.stats()["downloaded"] == 1


def test_failed_downloads_are_retried_next_cycle(tmp_path):
    store = make_store(tmp_path)
    store.record_download("vid00000001", playlist="p", title="T", uploader="U",
                          duration=None, path=None, status=STATUS_FAILED)
    # A captcha or a network blip must not permanently blacklist a track.
    assert store.downloaded_ids() == set()

    store.record_download("vid00000001", playlist="p", title="T", uploader="U",
                          duration=200.0, path="/m/T.m4a", status=STATUS_OK)
    assert store.downloaded_ids() == {"vid00000001"}
    assert store.stats() == {"downloaded": 1, "failed": 0, "skipped": 0, "library_files": 0}


def test_duplicates_after_download_still_count_as_handled(tmp_path):
    store = make_store(tmp_path)
    store.record_download("vid00000001", playlist="p", title="T", uploader="U",
                          duration=200.0, path="/m/.yt_duplicates/T.m4a",
                          status=STATUS_DUPLICATE_AFTER_DOWNLOAD)
    # Already dealt with, so it should never be fetched again.
    assert store.downloaded_ids() == {"vid00000001"}
    assert store.stats()["downloaded"] == 0


def test_skips_are_unique_per_video_and_status(tmp_path):
    store = make_store(tmp_path)
    for reason in ("first look", "second look"):
        store.record_skip("vid00000001", title="T", status="duplicate", reason=reason,
                          matched_path="/m/T.mp3", score=0.99, playlist="p")
    store.record_skip("vid00000001", title="T", status="too_short", reason="30s",
                      matched_path=None, score=0.0, playlist="p")
    assert store.skip_counts() == {"duplicate": 1, "too_short": 1}
    assert store.stats()["skipped"] == 2


def test_library_cache_round_trip_and_prune(tmp_path):
    store = make_store(tmp_path)
    store.save_library_files([
        ("/m/a.mp3", 100, 111, 200.0, None, '[["a","filename"]]'),
        ("/m/b.mp3", 200, 222, 180.0, "abc12345678", '[["b","filename"]]'),
    ])
    cached = store.load_library_cache()
    assert cached["/m/b.mp3"].video_id == "abc12345678"
    assert cached["/m/a.mp3"].duration == 200.0

    # Re-saving the same path updates rather than duplicating it.
    store.save_library_files([("/m/a.mp3", 101, 112, 201.0, None, '[["a","filename"]]')])
    assert store.load_library_cache()["/m/a.mp3"].size == 101
    assert store.stats()["library_files"] == 2

    assert store.prune_library_cache({"/m/a.mp3"}) == 1
    assert set(store.load_library_cache()) == {"/m/a.mp3"}
    assert store.save_library_files([]) is None


def test_recent_downloads_are_newest_first_and_ok_only(tmp_path):
    store = make_store(tmp_path)
    store.record_download("vid00000001", playlist="p", title="First", uploader="U",
                          duration=1.0, path="/m/1", status=STATUS_OK)
    store.record_download("vid00000002", playlist="p", title="Broken", uploader="U",
                          duration=None, path=None, status=STATUS_FAILED)
    store.record_download("vid00000003", playlist="p", title="Second", uploader="U",
                          duration=1.0, path="/m/3", status=STATUS_OK)
    titles = [row["title"] for row in store.recent_downloads(10)]
    assert "Broken" not in titles
    assert set(titles) == {"First", "Second"}
    assert len(store.recent_downloads(1)) == 1


def test_meta_survives_reopen(tmp_path):
    path = str(tmp_path / "state.db")
    store = Store(path)
    store.set_meta("last_run", "2026-01-01T00:00:00+00:00")
    store.set_meta("last_run", "2026-01-02T00:00:00+00:00")
    store.close()

    reopened = Store(path)
    assert reopened.get_meta("last_run") == "2026-01-02T00:00:00+00:00"
    assert reopened.get_meta("nothing", "fallback") == "fallback"
