"""SQLite storage for pantry items.

Mirrors the storage pattern used by Parcel Tracker elsewhere in this repo:
one file under /data, a context-managed connection per call, and a small
untyped `app_state` table for runtime settings and sync bookkeeping.
"""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "pantry.db"

STATUS_ACTIVE = "active"
STATUS_CONSUMED = "consumed"

# The public, stable vocabulary for "how urgent is this item" - used for the
# dashboard's grouping, the Home Assistant sensor state, and the
# notification decision. `unknown` covers an item with no expiry date at
# all (e.g. the use-by photo step was skipped).
BUCKET_OK = "ok"
BUCKET_EXPIRING_SOON = "expiring_soon"
BUCKET_EXPIRED = "expired"
BUCKET_UNKNOWN = "unknown"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                barcode TEXT,
                name TEXT NOT NULL,
                brand TEXT,
                quantity TEXT,
                category TEXT,
                expiry_date TEXT,
                notes TEXT,
                image_url TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                source TEXT NOT NULL DEFAULT 'manual',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                consumed_at TEXT,
                last_notified_at TEXT,
                last_notified_bucket TEXT
            )
            """
        )
        conn.execute("CREATE TABLE IF NOT EXISTS app_state (key TEXT PRIMARY KEY, value TEXT)")


def bucket_for(expiry_date: str | None, notify_days_before: int) -> str:
    """Which urgency bucket an expiry date falls into today. `notify_days_before`
    is the same threshold the notifier uses, so the dashboard's "expiring
    soon" grouping always matches what triggers a notification."""
    if not expiry_date:
        return BUCKET_UNKNOWN
    try:
        days_left = (date.fromisoformat(expiry_date) - date.today()).days
    except ValueError:
        return BUCKET_UNKNOWN
    if days_left < 0:
        return BUCKET_EXPIRED
    if days_left <= notify_days_before:
        return BUCKET_EXPIRING_SOON
    return BUCKET_OK


def days_left_for(expiry_date: str | None) -> int | None:
    if not expiry_date:
        return None
    try:
        return (date.fromisoformat(expiry_date) - date.today()).days
    except ValueError:
        return None


def add_item(
    *,
    barcode: str | None,
    name: str,
    brand: str = "",
    quantity: str = "",
    category: str = "",
    expiry_date: str | None = None,
    notes: str = "",
    image_url: str = "",
    source: str = "manual",
) -> int:
    now = now_iso()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO items (barcode, name, brand, quantity, category, expiry_date, notes, "
            "image_url, status, source, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                barcode,
                name,
                brand,
                quantity,
                category,
                expiry_date,
                notes,
                image_url,
                STATUS_ACTIVE,
                source,
                now,
                now,
            ),
        )
        return cur.lastrowid


def list_items(statuses: list[str] | None = None) -> list[dict]:
    with _connect() as conn:
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            rows = conn.execute(
                f"SELECT * FROM items WHERE status IN ({placeholders}) "
                "ORDER BY (expiry_date IS NULL), expiry_date ASC, created_at DESC",
                tuple(statuses),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM items ORDER BY (expiry_date IS NULL), expiry_date ASC, created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]


def get_item(item_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return dict(row) if row else None


def update_item(item_id: int, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = now_iso()
    assignments = ", ".join(f"{k} = ?" for k in fields)
    with _connect() as conn:
        conn.execute(f"UPDATE items SET {assignments} WHERE id = ?", (*fields.values(), item_id))


def consume_item(item_id: int) -> None:
    update_item(item_id, status=STATUS_CONSUMED, consumed_at=now_iso())


def delete_item(item_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM items WHERE id = ?", (item_id,))


def mark_notified(item_id: int, bucket: str) -> None:
    update_item(item_id, last_notified_at=now_iso(), last_notified_bucket=bucket)


def get_state(key: str, default: str | None = None) -> str | None:
    with _connect() as conn:
        row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_state(key: str, value: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
