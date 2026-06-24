import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "parcels.db"

STATUS_PENDING = "pending_confirmation"
STATUS_ACTIVE = "in_transit"
STATUS_EXCEPTION = "exception"
STATUS_DELIVERED = "delivered"
STATUS_DISMISSED = "dismissed"
STATUS_ARCHIVED = "archived"


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
            CREATE TABLE IF NOT EXISTS parcels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tracking_number TEXT NOT NULL UNIQUE,
                carrier_name TEXT,
                description TEXT,
                status TEXT NOT NULL DEFAULT 'pending_confirmation',
                status_detail TEXT,
                last_event_time TEXT,
                estimated_delivery TEXT,
                confidence REAL NOT NULL DEFAULT 0,
                source_message_id TEXT,
                email_sender TEXT,
                email_subject TEXT,
                email_body TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                delivered_at TEXT,
                archived_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_messages (
                message_id TEXT PRIMARY KEY,
                processed_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE TABLE IF NOT EXISTS app_state (key TEXT PRIMARY KEY, value TEXT)")

        # Added after the initial release - existing databases need these
        # columns added rather than created, since CREATE TABLE IF NOT
        # EXISTS is a no-op once the table already exists.
        existing_columns = {row["name"] for row in conn.execute("PRAGMA table_info(parcels)")}
        for column in ("email_sender", "email_subject", "email_body"):
            if column not in existing_columns:
                conn.execute(f"ALTER TABLE parcels ADD COLUMN {column} TEXT")


def is_processed(message_id: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM processed_messages WHERE message_id = ?", (message_id,)
        ).fetchone()
        return row is not None


def mark_processed(message_id: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO processed_messages (message_id, processed_at) VALUES (?, ?)",
            (message_id, now_iso()),
        )


def upsert_parcel(
    tracking_number: str,
    carrier_name: str,
    description: str,
    confidence: float,
    source_message_id: str | None,
    initial_status: str,
    email_sender: str | None = None,
    email_subject: str | None = None,
    email_body: str | None = None,
) -> int:
    now = now_iso()
    with _connect() as conn:
        existing = conn.execute(
            "SELECT id, confidence FROM parcels WHERE tracking_number = ?", (tracking_number,)
        ).fetchone()
        if existing:
            if confidence > existing["confidence"]:
                conn.execute(
                    "UPDATE parcels SET carrier_name = ?, description = ?, confidence = ?, "
                    "email_sender = ?, email_subject = ?, email_body = ?, updated_at = ? WHERE id = ?",
                    (
                        carrier_name,
                        description,
                        confidence,
                        email_sender,
                        email_subject,
                        email_body,
                        now,
                        existing["id"],
                    ),
                )
            else:
                conn.execute("UPDATE parcels SET updated_at = ? WHERE id = ?", (now, existing["id"]))
            return existing["id"]

        cur = conn.execute(
            "INSERT INTO parcels (tracking_number, carrier_name, description, status, "
            "confidence, source_message_id, email_sender, email_subject, email_body, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                tracking_number,
                carrier_name,
                description,
                initial_status,
                confidence,
                source_message_id,
                email_sender,
                email_subject,
                email_body,
                now,
                now,
            ),
        )
        return cur.lastrowid


def list_parcels(statuses=None) -> list[dict]:
    with _connect() as conn:
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            rows = conn.execute(
                f"SELECT * FROM parcels WHERE status IN ({placeholders}) ORDER BY updated_at DESC",
                tuple(statuses),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM parcels ORDER BY updated_at DESC").fetchall()
        return [dict(r) for r in rows]


def get_parcel(parcel_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM parcels WHERE id = ?", (parcel_id,)).fetchone()
        return dict(row) if row else None


def _set_fields(parcel_id: int, **fields) -> None:
    fields["updated_at"] = now_iso()
    assignments = ", ".join(f"{k} = ?" for k in fields)
    with _connect() as conn:
        conn.execute(f"UPDATE parcels SET {assignments} WHERE id = ?", (*fields.values(), parcel_id))


def confirm_parcel(parcel_id: int, carrier_name: str | None = None) -> None:
    fields = {"status": STATUS_ACTIVE}
    if carrier_name:
        fields["carrier_name"] = carrier_name
    _set_fields(parcel_id, **fields)


def dismiss_parcel(parcel_id: int) -> None:
    _set_fields(parcel_id, status=STATUS_DISMISSED, archived_at=now_iso())


def archive_parcel(parcel_id: int) -> None:
    _set_fields(parcel_id, status=STATUS_ARCHIVED, archived_at=now_iso())


def delete_parcel(parcel_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM parcels WHERE id = ?", (parcel_id,))


def parcels_needing_refresh() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM parcels WHERE status IN (?, ?)", (STATUS_ACTIVE, STATUS_EXCEPTION)
        ).fetchall()
        return [dict(r) for r in rows]


def update_tracking_status(
    parcel_id: int,
    status: str,
    status_detail: str | None,
    last_event_time: str | None,
    estimated_delivery: str | None,
) -> None:
    now = now_iso()
    delivered_at = now if status == STATUS_DELIVERED else None
    with _connect() as conn:
        conn.execute(
            "UPDATE parcels SET status=?, status_detail=?, last_event_time=?, "
            "estimated_delivery=?, updated_at=?, "
            "delivered_at=COALESCE(delivered_at, ?) WHERE id=?",
            (status, status_detail, last_event_time, estimated_delivery, now, delivered_at, parcel_id),
        )


def auto_archive_delivered(days: int) -> int:
    if days <= 0:
        return 0
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE parcels SET status = ?, archived_at = ? "
            "WHERE status = ? AND delivered_at IS NOT NULL "
            "AND datetime(delivered_at) <= datetime('now', ?)",
            (STATUS_ARCHIVED, now_iso(), STATUS_DELIVERED, f"-{days} days"),
        )
        return cur.rowcount


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
