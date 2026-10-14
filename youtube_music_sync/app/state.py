"""SQLite state: download history, skip reasons, and the library scan cache.

The library cache is what keeps a rescan cheap. Reading tags and durations for
a few thousand files over SMB is slow, so each file's metadata is stored
against its size and mtime, and only files that actually changed are re-read.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

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
    downloaded_at TEXT NOT NULL
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
        self._db.commit()

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
            "status=excluded.status, downloaded_at=excluded.downloaded_at",
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
