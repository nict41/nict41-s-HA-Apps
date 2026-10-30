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


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """Adds a column to an existing table if it's not already there - lets
    a column added in a later version show up on an add-on upgrade without
    losing any existing pantry data (CREATE TABLE IF NOT EXISTS alone only
    covers a brand new database)."""
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


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
                last_notified_bucket TEXT,
                has_date_photo INTEGER NOT NULL DEFAULT 0,
                date_extraction_pending INTEGER NOT NULL DEFAULT 0,
                date_extraction_raw TEXT
            )
            """
        )
        _ensure_column(conn, "items", "has_date_photo", "has_date_photo INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "items", "date_extraction_pending", "date_extraction_pending INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "items", "date_extraction_raw", "date_extraction_raw TEXT")
        conn.execute("CREATE TABLE IF NOT EXISTS app_state (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS barcode_cache (
                barcode TEXT PRIMARY KEY,
                name TEXT,
                brand TEXT,
                quantity TEXT,
                category TEXT,
                image_url TEXT,
                cached_at TEXT NOT NULL
            )
            """
        )


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
    delete_date_photo(item_id)


def date_photo_path(item_id: int) -> Path:
    # Derived from DATA_DIR on every call (rather than cached at import
    # time) so tests that monkeypatch DATA_DIR per-run land in the right
    # throwaway directory instead of the real add-on data dir.
    return DATA_DIR / "photos" / f"{item_id}.jpg"


def save_date_photo(item_id: int, image_bytes: bytes) -> None:
    path = date_photo_path(item_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(image_bytes)


def delete_date_photo(item_id: int) -> None:
    path = date_photo_path(item_id)
    if path.exists():
        path.unlink()


def start_date_extraction(item_id: int) -> None:
    """Marks an item as having a use-by date photo queued for background
    extraction. Called the moment the photo is saved, before the vision
    model has even been asked - see main.py's _extract_and_update."""
    update_item(item_id, has_date_photo=1, date_extraction_pending=1)


def finish_date_extraction(item_id: int, expiry_date: str | None, raw: str, *, only_if_blank: bool = False) -> None:
    """Called once background extraction completes. Only touches
    expiry_date when a date was actually found - a failed or "no date
    visible" read leaves whatever the item already had alone.

    `only_if_blank` (used when adding a brand new item with a photo
    attached) skips writing the found date if the item already has one -
    otherwise a date typed in by hand before tapping "Add to pantry" could
    get silently clobbered once the background read finishes, with no
    form left open to notice or correct it. Rescanning an existing item
    leaves this False, since overwriting whatever date was already there
    (right or wrong) is the whole point of a rescan.
    """
    fields = {"date_extraction_pending": 0, "date_extraction_raw": raw}
    if expiry_date:
        if only_if_blank:
            current = get_item(item_id)
            if current and not current.get("expiry_date"):
                fields["expiry_date"] = expiry_date
        else:
            fields["expiry_date"] = expiry_date
    update_item(item_id, **fields)


def mark_notified(item_id: int, bucket: str) -> None:
    update_item(item_id, last_notified_at=now_iso(), last_notified_bucket=bucket)


def get_cached_barcode(barcode: str) -> dict | None:
    """A previously looked-up product, by barcode. Product identity
    (name/brand/quantity/category) is effectively static once published, so
    this is cached indefinitely rather than TTL'd - a repeat scan of the
    same item (the common case in a pantry app: the same milk, the same
    tins, week after week) skips Open Food Facts entirely."""
    with _connect() as conn:
        row = conn.execute("SELECT * FROM barcode_cache WHERE barcode = ?", (barcode,)).fetchone()
        return dict(row) if row else None


def cache_barcode(barcode: str, product: dict) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO barcode_cache (barcode, name, brand, quantity, category, image_url, cached_at) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(barcode) DO UPDATE SET name = excluded.name, brand = excluded.brand, "
            "quantity = excluded.quantity, category = excluded.category, image_url = excluded.image_url, "
            "cached_at = excluded.cached_at",
            (
                barcode,
                product.get("name", ""),
                product.get("brand", ""),
                product.get("quantity", ""),
                product.get("category", ""),
                product.get("image_url", ""),
                now_iso(),
            ),
        )


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
