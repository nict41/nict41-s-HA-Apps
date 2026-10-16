"""SQLite state: download history, skip reasons, and the library scan cache.

The library cache is what keeps a rescan cheap. Reading tags and durations for
a few thousand files over SMB is slow, so each file's metadata is stored
against its size and mtime, and only files that actually changed are re-read.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS library_cache (
    path      TEXT PRIMARY KEY,
    size      INTEGER NOT NULL,
    mtime_ns  INTEGER NOT NULL,
    duration  REAL,
    video_id  TEXT,
    variants  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS downloads (
    video_id      TEXT PRIMARY KEY,
    playlist      TEXT,
    title         TEXT,
    uploader      TEXT,
    duration      REAL,
    path          TEXT,
    status        TEXT NOT NULL,
    downloaded_at TEXT NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    retry_after   TEXT,
    last_error    TEXT
);
CREATE TABLE IF NOT EXISTS skips (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id     TEXT,
    title        TEXT,
    status       TEXT NOT NULL,
    reason       TEXT,
    matched_path TEXT,
    score        REAL,
    playlist     TEXT,
    seen_at      TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS skips_video_status
    ON skips (video_id, status);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_DUPLICATE_AFTER_DOWNLOAD = "duplicate_after_download"

# How long to leave a video alone after each consecutive "unavailable"
# failure, in hours: six hours, a day, three days, a week, then a month.
#
# Deliberately a backoff rather than a blacklist. An unavailable video is not
# always gone for good - a private video can be made public again, a
# geo-block can lift, a copyright claim can be resolved, and an age-gated one
# starts working the moment cookies_file is set. A month between attempts
# costs one request and keeps that door open.
UNAVAILABLE_BACKOFF_HOURS = (6, 24, 72, 168, 720)

# Every *other* failure (a captcha that didn't clear, a stale-partial 416, a
# dropped connection that keeps dropping, ...) used to retry every single
# cycle forever. That is fine for one or two attempts - most of these really
# are transient - but a video that keeps failing the same way for days is
# consuming a download slot and a request every cycle for nothing. Same
# shape as the unavailable backoff, just shorter: an hour, six hours, a day,
# then the same recheck_unavailable_days cap.
TRANSIENT_BACKOFF_HOURS = (1, 6, 24)

# How long YouTube's own bot/rate-limit challenge pauses every cycle for, in
# hours: starts at BOT_BLOCK_BACKOFF_START_HOURS and doubles on each cycle
# that is still blocked when it would otherwise have run, up to this cap.
BOT_BLOCK_BACKOFF_START_HOURS = 6
BOT_BLOCK_BACKOFF_CAP_HOURS = 24


def _retry_after(attempts: int, cap_days: int, schedule_hours: tuple[int, ...]) -> str | None:
    """When a video that has failed `attempts` times should be tried again.

    The first len(schedule_hours) attempts follow that schedule; every
    attempt after that waits `cap_days` days. `cap_days` of 0 means never:
    the video is retired until the database is cleared or it stops
    appearing in a playlist.
    """
    if cap_days == 0:
        return None
    if attempts <= len(schedule_hours):
        hours = schedule_hours[attempts - 1]
    else:
        hours = cap_days * 24
    # A schedule entry can exceed a cap_days set low by the user - never wait
    # longer than they asked for, even on an early attempt.
    hours = min(hours, cap_days * 24)
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="seconds")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class CachedFile:
    size: int
    mtime_ns: int
    duration: float | None
    video_id: str | None
    variants: str


class Store:
    def __init__(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._migrate()
        self._db.commit()

    def _migrate(self) -> None:
        """Add columns a database created by an earlier version is missing.

        `CREATE TABLE IF NOT EXISTS` leaves an existing table exactly as it
        was, so the retry-backoff columns have to be added explicitly.
        """
        existing = {row["name"] for row in self._db.execute("PRAGMA table_info(downloads)")}
        for column, definition in (
            ("attempts", "INTEGER NOT NULL DEFAULT 0"),
            ("retry_after", "TEXT"),
            ("last_error", "TEXT"),
        ):
            if column not in existing:
                self._db.execute(f"ALTER TABLE downloads ADD COLUMN {column} {definition}")

    def close(self) -> None:
        self._db.close()

    # ---------------------------------------------------------------- library

    def load_library_cache(self) -> dict[str, CachedFile]:
        rows = self._db.execute(
            "SELECT path, size, mtime_ns, duration, video_id, variants FROM library_cache"
        ).fetchall()
        return {
            row["path"]: CachedFile(
                size=row["size"],
                mtime_ns=row["mtime_ns"],
                duration=row["duration"],
                video_id=row["video_id"],
                variants=row["variants"],
            )
            for row in rows
        }

    def save_library_files(self, rows: list[tuple[str, int, int, float | None, str | None, str]]) -> None:
        if not rows:
            return
        self._db.executemany(
            "INSERT INTO library_cache (path, size, mtime_ns, duration, video_id, variants) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime_ns=excluded.mtime_ns, "
            "duration=excluded.duration, video_id=excluded.video_id, variants=excluded.variants",
            rows,
        )
        self._db.commit()

    def prune_library_cache(self, keep: set[str]) -> int:
        existing = {row["path"] for row in self._db.execute("SELECT path FROM library_cache")}
        gone = existing - keep
        if gone:
            self._db.executemany(
                "DELETE FROM library_cache WHERE path = ?", [(p,) for p in gone]
            )
            self._db.commit()
        return len(gone)

    # -------------------------------------------------------------- downloads

    def downloaded_ids(self) -> set[str]:
        """Video ids already accounted for, so they are never re-fetched.

        Failures are deliberately excluded: a download that failed because
        YouTube threw a captcha should be retried on the next cycle.
        """
        return {
            row["video_id"]
            for row in self._db.execute(
                "SELECT video_id FROM downloads WHERE status != ?", (STATUS_FAILED,)
            )
        }

    def deferred_ids(self) -> dict[str, str]:
        """Videos not due to be retried yet, mapped to when they next are.

        A download that failed because YouTube says the video is unavailable
        is backed off rather than repeated every cycle: re-reading the same
        dead video every hour burns a `max_downloads_per_cycle` slot and a
        request against the rate limit that earns nothing.
        """
        now = _now()
        return {
            row["video_id"]: row["retry_after"]
            for row in self._db.execute(
                "SELECT video_id, retry_after FROM downloads "
                "WHERE status = ? AND retry_after IS NOT NULL AND retry_after > ?",
                (STATUS_FAILED, now),
            )
        }

    def retired_ids(self) -> set[str]:
        """Videos retired for good: failed, unavailable, and never due again.

        Only reachable with the re-check window set to 0, which is the
        explicit "stop asking entirely" setting.
        """
        return {
            row["video_id"]
            for row in self._db.execute(
                "SELECT video_id FROM downloads "
                "WHERE status = ? AND retry_after IS NULL AND attempts > 0",
                (STATUS_FAILED,),
            )
        }

    def record_failure(
        self,
        video_id: str,
        *,
        playlist: str,
        title: str,
        uploader: str,
        duration: float | None,
        permanent: bool,
        error: str,
        cap_days: int,
    ) -> tuple[int, str | None]:
        """Record a failed download and decide when to try it again.

        Every failure escalates the backoff, permanent or not - a video that
        keeps failing, for whatever reason, is backed off further each time
        rather than retried every cycle forever. An unavailable classification
        just escalates faster (UNAVAILABLE_BACKOFF_HOURS) than an ordinary one
        (TRANSIENT_BACKOFF_HOURS); switching between the two classifications
        from one attempt to the next does not reset the count, only which
        schedule the next wait is read from.
        Returns (attempts so far, when it is next due) for logging.
        """
        row = self._db.execute(
            "SELECT attempts FROM downloads WHERE video_id = ?", (video_id,)
        ).fetchone()
        previous = int(row["attempts"]) if row and row["attempts"] else 0
        attempts = previous + 1
        schedule = UNAVAILABLE_BACKOFF_HOURS if permanent else TRANSIENT_BACKOFF_HOURS
        retry_after = _retry_after(attempts, cap_days, schedule)

        self._db.execute(
            "INSERT INTO downloads (video_id, playlist, title, uploader, duration, path, "
            "status, downloaded_at, attempts, retry_after, last_error) "
            "VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?) "
            "ON CONFLICT(video_id) DO UPDATE SET playlist=excluded.playlist, title=excluded.title, "
            "uploader=excluded.uploader, duration=excluded.duration, path=NULL, "
            "status=excluded.status, downloaded_at=excluded.downloaded_at, "
            "attempts=excluded.attempts, retry_after=excluded.retry_after, "
            "last_error=excluded.last_error",
            (video_id, playlist, title, uploader, duration, STATUS_FAILED, _now(),
             attempts, retry_after, error[:500]),
        )
        self._db.commit()
        return attempts, retry_after

    def record_download(
        self,
        video_id: str,
        *,
        playlist: str,
        title: str,
        uploader: str,
        duration: float | None,
        path: str | None,
        status: str,
    ) -> None:
        self._db.execute(
            "INSERT INTO downloads (video_id, playlist, title, uploader, duration, path, status, downloaded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(video_id) DO UPDATE SET playlist=excluded.playlist, title=excluded.title, "
            "uploader=excluded.uploader, duration=excluded.duration, path=excluded.path, "
            "status=excluded.status, downloaded_at=excluded.downloaded_at, "
            "attempts=0, retry_after=NULL, last_error=NULL",
            (video_id, playlist, title, uploader, duration, path, status, _now()),
        )
        self._db.commit()

    def record_skip(
        self,
        video_id: str,
        *,
        title: str,
        status: str,
        reason: str,
        matched_path: str | None,
        score: float,
        playlist: str,
    ) -> None:
        self._db.execute(
            "INSERT INTO skips (video_id, title, status, reason, matched_path, score, playlist, seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(video_id, status) DO UPDATE SET title=excluded.title, reason=excluded.reason, "
            "matched_path=excluded.matched_path, score=excluded.score, playlist=excluded.playlist, "
            "seen_at=excluded.seen_at",
            (video_id, title, status, reason, matched_path, score, playlist, _now()),
        )
        self._db.commit()

    def recent_downloads(self, limit: int = 10) -> list[dict]:
        rows = self._db.execute(
            "SELECT title, path, downloaded_at FROM downloads WHERE status = ? "
            "ORDER BY downloaded_at DESC, rowid DESC LIMIT ?",
            (STATUS_OK, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def stats(self) -> dict[str, int]:
        def scalar(sql: str, args: tuple = ()) -> int:
            return int(self._db.execute(sql, args).fetchone()[0] or 0)

        return {
            "downloaded": scalar("SELECT COUNT(*) FROM downloads WHERE status = ?", (STATUS_OK,)),
            "failed": scalar("SELECT COUNT(*) FROM downloads WHERE status = ?", (STATUS_FAILED,)),
            "skipped": scalar("SELECT COUNT(*) FROM skips"),
            "library_files": scalar("SELECT COUNT(*) FROM library_cache"),
        }

    def skip_counts(self) -> dict[str, int]:
        return {
            row["status"]: row["n"]
            for row in self._db.execute(
                "SELECT status, COUNT(*) AS n FROM skips GROUP BY status"
            )
        }

    # ------------------------------------------------------------------- meta

    def set_meta(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self._db.commit()

    def get_meta(self, key: str, default: str = "") -> str:
        row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    # ---------------------------------------------------- bot-block backoff

    def register_bot_block(self) -> str:
        """Record a YouTube bot/rate-limit challenge. Returns the ISO
        timestamp cycles are paused until.

        Starts at BOT_BLOCK_BACKOFF_START_HOURS; a hit while an earlier
        block is still in effect (i.e. consecutive, not a fresh one after
        things recovered) doubles the wait, up to BOT_BLOCK_BACKOFF_CAP_HOURS.
        Survives a restart: both the level and the deadline live here, not
        in memory.
        """
        current = int(self.get_meta("bot_backoff_hours", "0") or 0)
        still_blocked = self.bot_backoff_until() is not None
        if current and still_blocked:
            hours = min(current * 2, BOT_BLOCK_BACKOFF_CAP_HOURS)
        else:
            hours = BOT_BLOCK_BACKOFF_START_HOURS
        until = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="seconds")
        self.set_meta("bot_backoff_hours", str(hours))
        self.set_meta("bot_backoff_until", until)
        return until

    def bot_backoff_until(self) -> str | None:
        """ISO timestamp cycles are paused until, or None if not in effect."""
        until = self.get_meta("bot_backoff_until", "")
        if until and until > _now():
            return until
        return None

    def clear_bot_backoff(self) -> None:
        """Reset the escalation level once a cycle gets through clean.

        Otherwise a single old block from weeks ago would keep doubling the
        starting point of any future one forever.
        """
        self.set_meta("bot_backoff_hours", "0")
