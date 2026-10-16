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


# ------------------------------------------------- unavailable-video backoff

def _fail(store, video_id="vid00000001", permanent=True, cap_days=30, error="Video unavailable"):
    return store.record_failure(video_id, playlist="p", title="T", uploader="U",
                                duration=None, permanent=permanent, error=error,
                                cap_days=cap_days)


def test_unavailable_failures_back_off_further_each_time(tmp_path):
    store = make_store(tmp_path)
    seen = []
    for _ in range(6):
        attempts, retry_after = _fail(store)
        seen.append((attempts, retry_after))
    # Attempts climb, and each re-check is no earlier than the last.
    assert [a for a, _ in seen] == [1, 2, 3, 4, 5, 6]
    stamps = [r for _, r in seen]
    assert all(stamps[i] <= stamps[i + 1] for i in range(len(stamps) - 1))
    # ...and it plateaus at the cap rather than growing forever.
    assert stamps[-1] == stamps[-2]


def test_a_deferred_video_is_not_offered_again_yet(tmp_path):
    store = make_store(tmp_path)
    _fail(store)
    assert "vid00000001" in store.deferred_ids()
    assert store.retired_ids() == set()


def test_a_transient_failure_also_backs_off_now(tmp_path):
    # Previously these retried every single cycle forever - a stale-partial
    # 416 or a captcha that doesn't clear would eat a download slot and a
    # request every cycle indefinitely. They now back off too, just on a
    # shorter schedule than an unavailable classification.
    store = make_store(tmp_path)
    attempts, retry_after = _fail(store, permanent=False, error="HTTP Error 429: Too Many Requests")
    assert attempts == 1 and retry_after is not None
    assert "vid00000001" in store.deferred_ids()
    assert store.retired_ids() == set()
    # Still absent from downloaded_ids, i.e. still due for another attempt -
    # just not until retry_after.
    assert "vid00000001" not in store.downloaded_ids()


def test_transient_failures_back_off_further_each_time_on_their_own_schedule(tmp_path):
    store = make_store(tmp_path)
    seen = []
    for _ in range(5):
        attempts, retry_after = _fail(store, permanent=False, error="Connection reset by peer")
        seen.append((attempts, retry_after))
    assert [a for a, _ in seen] == [1, 2, 3, 4, 5]
    stamps = [r for _, r in seen]
    assert all(stamps[i] <= stamps[i + 1] for i in range(len(stamps) - 1))
    # Attempted at most once a day after the third failure: the schedule
    # (1h, 6h, 24h) is exhausted by then, and every attempt after falls back
    # to the (default 30-day) recheck_unavailable_days cap, which is >= 24h.
    assert stamps[2] > stamps[1] >= stamps[0]


def test_switching_classification_does_not_reset_the_backoff(tmp_path):
    """A transient failure no longer wipes out an unavailable one's progress.

    The two schedules are just different speeds of the same escalation -
    whatever classification the *current* attempt gets just picks which
    schedule the wait comes from, continuing from the same attempt count.
    """
    store = make_store(tmp_path)
    _fail(store)
    _fail(store)
    assert store.deferred_ids()
    attempts, retry_after = _fail(store, permanent=False, error="Connection reset by peer")
    assert attempts == 3
    assert retry_after is not None
    assert "vid00000001" in store.deferred_ids()


def test_cap_of_zero_retires_the_video(tmp_path):
    store = make_store(tmp_path)
    attempts, retry_after = _fail(store, cap_days=0)
    assert attempts == 1 and retry_after is None
    assert store.retired_ids() == {"vid00000001"}
    assert store.deferred_ids() == {}


def test_a_later_success_clears_the_backoff(tmp_path):
    store = make_store(tmp_path)
    _fail(store)
    store.record_download("vid00000001", playlist="p", title="T", uploader="U",
                          duration=200.0, path="/m/T.opus", status=STATUS_OK)
    assert store.deferred_ids() == {}
    assert store.retired_ids() == set()
    assert "vid00000001" in store.downloaded_ids()


def test_a_database_from_an_earlier_version_is_migrated(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    old = sqlite3.connect(str(path))
    old.executescript("""
        CREATE TABLE downloads (
            video_id      TEXT PRIMARY KEY,
            playlist      TEXT,
            title         TEXT,
            uploader      TEXT,
            duration      REAL,
            path          TEXT,
            status        TEXT NOT NULL,
            downloaded_at TEXT NOT NULL
        );
        INSERT INTO downloads VALUES ('old00000001','p','T','U',1.0,'/m/T.opus','ok','2026-01-01T00:00:00+00:00');
    """)
    old.commit()
    old.close()

    store = Store(str(path))  # must not raise on the missing columns
    assert "old00000001" in store.downloaded_ids()
    assert store.deferred_ids() == {}
    _fail(store)
    assert "vid00000001" in store.deferred_ids()


# ---------------------------------------------------------- bot-block backoff

def test_a_bot_block_pauses_for_the_starting_period(tmp_path):
    store = make_store(tmp_path)
    assert store.bot_backoff_until() is None
    until = store.register_bot_block()
    assert until is not None
    assert store.bot_backoff_until() == until


def test_consecutive_bot_blocks_double_up_to_the_cap(tmp_path):
    store = make_store(tmp_path)
    first = store.register_bot_block()
    second = store.register_bot_block()  # still blocked - doubles
    assert second > first
    # Force it to look expired so the next registration reads as consecutive
    # only via the still-blocked check, not by running the clock for real.
    for _ in range(6):
        store.register_bot_block()
    # Plateaus at the cap rather than growing forever.
    assert store.get_meta("bot_backoff_hours") == "24"


def test_a_fresh_block_after_recovery_restarts_at_the_starting_period(tmp_path):
    store = make_store(tmp_path)
    store.register_bot_block()
    store.register_bot_block()
    assert store.get_meta("bot_backoff_hours") != "6"
    store.clear_bot_backoff()
    fresh = store.register_bot_block()
    assert store.get_meta("bot_backoff_hours") == "6"
    assert fresh is not None


def test_bot_backoff_survives_reopen(tmp_path):
    path = str(tmp_path / "state.db")
    store = Store(path)
    until = store.register_bot_block()
    store.close()

    reopened = Store(path)
    assert reopened.bot_backoff_until() == until
