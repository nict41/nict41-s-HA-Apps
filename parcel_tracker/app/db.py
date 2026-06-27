import json
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
                email_html TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                delivered_at TEXT,
                archived_at TEXT,
                provider_confirmed INTEGER NOT NULL DEFAULT 0,
                first_checked_at TEXT,
                last_checked_at TEXT
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
        for column in ("email_sender", "email_subject", "email_body", "email_html"):
            if column not in existing_columns:
                conn.execute(f"ALTER TABLE parcels ADD COLUMN {column} TEXT")
        if "tracking_provider" not in existing_columns:
            conn.execute("ALTER TABLE parcels ADD COLUMN tracking_provider TEXT")
            # Parcels already being tracked before this column existed were
            # always registered with 17track (the only provider available at
            # the time) - backfill so they keep using it instead of being
            # re-registered with a newly-added second provider.
            conn.execute(
                "UPDATE parcels SET tracking_provider = '17track' WHERE status IN (?, ?)",
                (STATUS_ACTIVE, STATUS_EXCEPTION),
            )
        if "provider_confirmed" not in existing_columns:
            conn.execute("ALTER TABLE parcels ADD COLUMN provider_confirmed INTEGER NOT NULL DEFAULT 0")
        if "first_checked_at" not in existing_columns:
            # Left NULL (rather than backfilled to created_at) so that a
            # parcel which predates this column - or one only just registered
            # with a newly-added/changed provider - gets a fresh grace period
            # starting from its first real check, instead of immediately
            # qualifying for auto-dismissal because it happens to be old.
            conn.execute("ALTER TABLE parcels ADD COLUMN first_checked_at TEXT")
        if "tracking_history" not in existing_columns:
            conn.execute("ALTER TABLE parcels ADD COLUMN tracking_history TEXT")
        if "last_checked_at" not in existing_columns:
            # When a tracking provider last returned a result for this parcel,
            # so the provider-refresh phase can throttle how often each parcel
            # is re-queried independently of how often mail is checked. Left
            # NULL on existing rows so a never-checked parcel is always due.
            conn.execute("ALTER TABLE parcels ADD COLUMN last_checked_at TEXT")


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
    email_html: str | None = None,
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
                    "email_sender = ?, email_subject = ?, email_body = ?, email_html = ?, "
                    "updated_at = ? WHERE id = ?",
                    (
                        carrier_name,
                        description,
                        confidence,
                        email_sender,
                        email_subject,
                        email_body,
                        email_html,
                        now,
                        existing["id"],
                    ),
                )
            else:
                conn.execute("UPDATE parcels SET updated_at = ? WHERE id = ?", (now, existing["id"]))
            return existing["id"]

        cur = conn.execute(
            "INSERT INTO parcels (tracking_number, carrier_name, description, status, "
            "confidence, source_message_id, email_sender, email_subject, email_body, email_html, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
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
                email_html,
                now,
                now,
            ),
        )
        return cur.lastrowid


def _row_to_parcel(row: sqlite3.Row) -> dict:
    """tracking_history is stored as a JSON string (see update_tracking_status)
    so it round-trips through SQLite like any other TEXT column - callers
    want it back as the list it actually represents.

    The raw `email_html` is deliberately dropped here so it never rides along
    with the general parcel dict: it can be hundreds of KB per parcel and
    would needlessly bloat `/api/parcels` (which the Lovelace card fetches
    cross-origin) and `/export`. The email viewer reads it on demand via
    get_parcel_email() instead."""
    parcel = dict(row)
    raw_history = parcel.get("tracking_history")
    parcel["tracking_history"] = json.loads(raw_history) if raw_history else []
    # A cheap boolean so the dashboard knows whether to offer "View email"
    # without carrying the (potentially large) html/body along for the ride.
    parcel["has_email"] = bool(parcel.get("email_html") or parcel.get("email_body"))
    parcel.pop("email_html", None)
    return parcel


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
        return [_row_to_parcel(r) for r in rows]


def get_parcel(parcel_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM parcels WHERE id = ?", (parcel_id,)).fetchone()
        return _row_to_parcel(row) if row else None


def get_parcel_email(parcel_id: int) -> dict | None:
    """The source email for one parcel, including the raw `email_html` that
    _row_to_parcel strips from the general dict - read on demand by the
    email viewer route. Returns None if the parcel doesn't exist."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT email_sender, email_subject, email_body, email_html FROM parcels WHERE id = ?",
            (parcel_id,),
        ).fetchone()
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


def reset_parcel(parcel_id: int) -> None:
    """Puts a parcel back to a freshly-detected state, so it goes through
    confirmation and tracking-provider lookup again from scratch - for when
    a parcel's status/carrier ended up wrong and needs a do-over rather than
    a delete-and-re-add.

    If it came from an email, that email's Message-ID is cleared from
    processed_messages too, so the next "Check mail now" re-scans it and
    re-runs detection on its actual content instead of leaving the parcel's
    stale fields untouched (a re-scanned message that's still marked
    processed would otherwise just be skipped). confidence is reset to 0 so
    that re-detection - whatever confidence it comes back with - is always
    treated as an improvement and allowed to overwrite carrier_name/
    description, the same way a brand new candidate would."""
    parcel = get_parcel(parcel_id)
    if not parcel:
        return
    with _connect() as conn:
        if parcel["source_message_id"]:
            conn.execute(
                "DELETE FROM processed_messages WHERE message_id = ?", (parcel["source_message_id"],)
            )
        conn.execute(
            "UPDATE parcels SET status = ?, status_detail = NULL, last_event_time = NULL, "
            "estimated_delivery = NULL, confidence = 0, provider_confirmed = 0, "
            "first_checked_at = NULL, tracking_provider = NULL, delivered_at = NULL, "
            "archived_at = NULL, tracking_history = NULL, updated_at = ? WHERE id = ?",
            (STATUS_PENDING, now_iso(), parcel_id),
        )


def reset_all_data() -> None:
    """Wipes every tracked parcel and mail-sync bookkeeping, for a full
    factory reset. Irreversible - callers are responsible for confirming
    this is really what's wanted before calling it."""
    with _connect() as conn:
        conn.execute("DELETE FROM parcels")
        conn.execute("DELETE FROM processed_messages")
        conn.execute("DELETE FROM app_state")


def parcels_needing_refresh() -> list[dict]:
    """Pending candidates are included too, so the provider can correct their
    carrier guess and auto-confirm them once it positively recognises the
    number. Until a candidate is recognised, callers should leave its `status`
    untouched (pass status=None to update_tracking_status) so an unrecognised
    guess still waits for an explicit confirm/dismiss."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM parcels WHERE status IN (?, ?, ?)",
            (STATUS_PENDING, STATUS_ACTIVE, STATUS_EXCEPTION),
        ).fetchall()
        return [_row_to_parcel(r) for r in rows]


def update_tracking_status(
    parcel_id: int,
    status: str | None,
    status_detail: str | None,
    last_event_time: str | None,
    estimated_delivery: str | None,
    carrier_name: str | None = None,
    tracking_provider: str | None = None,
    confirmed: bool = False,
    events: list[dict] | None = None,
) -> None:
    """status=None leaves the parcel's lifecycle status untouched - used for
    pending candidates, where only a preview is wanted, not auto-confirmation.

    status_detail/last_event_time/estimated_delivery/events only overwrite
    their stored value when the provider actually returned one on *this*
    check - a momentary gap in the provider's response (rate limiting, a
    not-yet-indexed registration, a parsing edge case) shouldn't blank out
    previously-known-good status text (or the journey built up so far) just
    because this particular refresh came back empty.

    `confirmed` reflects whether the provider positively recognised the
    number on *this* check. `first_checked_at` is stamped on every call
    (it's only ever called once a provider has actually returned a result for
    this number), and `provider_confirmed` is sticky - once a provider has
    ever confirmed a parcel it stays confirmed, so a later inconclusive check
    can't undo it."""
    now = now_iso()
    assignments = [
        "status_detail = COALESCE(?, status_detail)",
        "last_event_time = COALESCE(?, last_event_time)",
        "estimated_delivery = COALESCE(?, estimated_delivery)",
        "updated_at = ?",
        "first_checked_at = COALESCE(first_checked_at, ?)",
        # Always stamped: this function is only called once a provider has
        # actually returned a result for the number, so it marks the most
        # recent provider check (used to throttle re-checks).
        "last_checked_at = ?",
    ]
    params = [status_detail, last_event_time, estimated_delivery, now, now, now]
    if status is not None:
        assignments += ["status = ?", "delivered_at = COALESCE(delivered_at, ?)"]
        params += [status, now if status == STATUS_DELIVERED else None]
    if carrier_name:
        assignments.append("carrier_name = ?")
        params.append(carrier_name)
    if tracking_provider:
        assignments.append("tracking_provider = ?")
        params.append(tracking_provider)
    if confirmed:
        assignments.append("provider_confirmed = 1")
    if events:
        assignments.append("tracking_history = ?")
        params.append(json.dumps(events))
    params.append(parcel_id)
    with _connect() as conn:
        conn.execute(f"UPDATE parcels SET {', '.join(assignments)} WHERE id = ?", params)


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
