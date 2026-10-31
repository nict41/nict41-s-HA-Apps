"""SQLite storage for pantry items.

Mirrors the storage pattern used by Parcel Tracker elsewhere in this repo:
one file under /data, a context-managed connection per call, and a small
untyped `app_state` table for runtime settings and sync bookkeeping.
"""

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "pantry.db"

STATUS_ACTIVE = "active"
STATUS_CONSUMED = "consumed"

# How far a use-by date read from a photo can be trusted.
CONFIDENCE_CONFIRMED = "confirmed"  # both readers found this date
CONFIDENCE_CHECK = "check"  # only one reader found a date
CONFIDENCE_CONFLICT = "conflict"  # the readers disagree, see date_candidates
CONFIDENCE_UNREAD = "unread"  # neither reader found a date

# The public, stable vocabulary for "how urgent is this item" - used for the
# dashboard's grouping, the Home Assistant sensor state, and the
# notification decision. `unknown` covers an item with no expiry date at
# all (e.g. the use-by photo step was skipped).
BUCKET_OK = "ok"
BUCKET_EXPIRING_SOON = "expiring_soon"
BUCKET_EXPIRED = "expired"
BUCKET_UNKNOWN = "unknown"
BUCKET_FROZEN = "frozen"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def local_zone():
    """Returns the configured local zone, or UTC for a missing or bad TZ."""
    try:
        value = os.environ.get("TZ", "UTC").strip()
        return ZoneInfo(value) if value else timezone.utc
    except Exception:
        return timezone.utc


def today() -> date:
    return datetime.now(local_zone()).date()


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
                date_extraction_raw TEXT,
                date_ocr_date TEXT,
                date_ocr_raw TEXT,
                date_ocr_candidates TEXT,
                date_confidence TEXT,
                date_candidates TEXT
            )
            """
        )
        _ensure_column(conn, "items", "has_date_photo", "has_date_photo INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "items", "date_extraction_pending", "date_extraction_pending INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "items", "date_extraction_raw", "date_extraction_raw TEXT")
        _ensure_column(conn, "items", "date_ocr_date", "date_ocr_date TEXT")
        _ensure_column(conn, "items", "date_ocr_raw", "date_ocr_raw TEXT")
        _ensure_column(conn, "items", "date_ocr_candidates", "date_ocr_candidates TEXT")
        _ensure_column(conn, "items", "date_confidence", "date_confidence TEXT")
        _ensure_column(conn, "items", "date_candidates", "date_candidates TEXT")
        for column, ddl in (
            ("count", "count INTEGER NOT NULL DEFAULT 1"),
            ("location", "location TEXT"),
            ("opened_at", "opened_at TEXT"),
            ("use_within_days", "use_within_days INTEGER"),
            ("defrosted_at", "defrosted_at TEXT"),
            ("snoozed_until", "snoozed_until TEXT"),
            ("consumed_outcome", "consumed_outcome TEXT"),
        ):
            _ensure_column(conn, "items", column, ddl)
        conn.execute("CREATE TABLE IF NOT EXISTS app_state (key TEXT PRIMARY KEY, value TEXT)")
        # notify_service moved to add-on-configuration-only (settings.py) -
        # a value saved here from an older version would otherwise go on
        # silently shadowing the add-on's Configuration tab forever, which
        # is exactly the "my notification service keeps getting lost"
        # symptom this cleans up.
        conn.execute("DELETE FROM app_state WHERE key = 'setting:notify_service'")
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
        _ensure_column(conn, "barcode_cache", "location", "location TEXT")
        _ensure_column(conn, "barcode_cache", "opened_days", "opened_days INTEGER")
        _ensure_column(conn, "barcode_cache", "suggested_location", "suggested_location TEXT")
        conn.execute("""CREATE TABLE IF NOT EXISTS item_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER NOT NULL,
            barcode TEXT, name TEXT NOT NULL, category TEXT, outcome TEXT NOT NULL,
            created_at TEXT NOT NULL)""")


def _parse_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError):
        return None


def effective_date(item: dict) -> str | None:
    if item.get("location") == "freezer":
        return None
    candidates = []
    defrosted = _parse_date(item.get("defrosted_at"))
    expiry = _parse_date(item.get("expiry_date"))
    if defrosted:
        candidates.append((date.fromisoformat(defrosted) + timedelta(days=1)).isoformat())
    elif expiry:
        candidates.append(expiry)
    opened = _parse_date(item.get("opened_at"))
    try:
        days = int(item.get("use_within_days"))
    except (TypeError, ValueError):
        days = 0
    if opened and days > 0:
        candidates.append((date.fromisoformat(opened) + timedelta(days=days)).isoformat())
    return min(candidates) if candidates else None


def bucket_for(expiry_date: str | dict | None, notify_days_before: int) -> str:
    """Which urgency bucket an expiry date falls into today. `notify_days_before`
    is the same threshold the notifier uses, so the dashboard's "expiring
    soon" grouping always matches what triggers a notification."""
    if isinstance(expiry_date, dict):
        if expiry_date.get("location") == "freezer":
            return BUCKET_FROZEN
        expiry_date = effective_date(expiry_date)
    if not expiry_date:
        return BUCKET_UNKNOWN
    try:
        days_left = (date.fromisoformat(expiry_date) - today()).days
    except ValueError:
        return BUCKET_UNKNOWN
    if days_left < 0:
        return BUCKET_EXPIRED
    if days_left <= notify_days_before:
        return BUCKET_EXPIRING_SOON
    return BUCKET_OK


def days_left_for(expiry_date: str | dict | None) -> int | None:
    if isinstance(expiry_date, dict):
        expiry_date = effective_date(expiry_date)
    if not expiry_date:
        return None
    try:
        return (date.fromisoformat(expiry_date) - today()).days
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
    count: int = 1,
    location: str | None = None,
    opened_at: str | None = None,
    use_within_days: int | None = None,
) -> int:
    now = now_iso()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO items (barcode, name, brand, quantity, category, expiry_date, notes, "
            "image_url, status, source, created_at, updated_at, count, location, opened_at, use_within_days) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                count,
                location,
                opened_at,
                use_within_days,
            ),
        )
        return cur.lastrowid


def list_items(statuses: list[str] | None = None) -> list[dict]:
    with _connect() as conn:
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            rows = conn.execute(
                f"SELECT * FROM items WHERE status IN ({placeholders}) "
                "ORDER BY created_at DESC",
                tuple(statuses),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM items ORDER BY created_at DESC"
            ).fetchall()
        items = [dict(r) for r in rows]
        return sorted(
            items,
            key=lambda item: (effective_date(item) is None, effective_date(item) or "", -item["id"]),
        )


def get_item(item_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return dict(row) if row else None


def pending_date_extractions() -> list[int]:
    """Returns queued reads whose saved photo is still present."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id FROM items WHERE date_extraction_pending = 1",
        ).fetchall()
    return [row["id"] for row in rows if date_photo_path(row["id"]).exists()]


def update_item(item_id: int, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = now_iso()
    assignments = ", ".join(f"{k} = ?" for k in fields)
    with _connect() as conn:
        conn.execute(f"UPDATE items SET {assignments} WHERE id = ?", (*fields.values(), item_id))


def transition_item(item_id: int, *, opened: bool | None = None, location: str | None = None,
                    use_within_days: int | None = None, opened_at: str | None = None) -> dict | None:
    item = get_item(item_id)
    if not item:
        return None
    fields = {}
    if opened is not None:
        fields["opened_at"] = (opened_at or today().isoformat()) if opened else None
        fields["use_within_days"] = use_within_days if opened else None
    if location is not None:
        previous = item.get("location") or ""
        fields["location"] = location or None
        if location == "freezer":
            fields["defrosted_at"] = None
        elif previous == "freezer":
            fields["defrosted_at"] = today().isoformat()
    if fields:
        update_item(item_id, **fields)
    return get_item(item_id)


def freeze_item(item_id: int) -> dict | None:
    item = get_item(item_id)
    if not item or item["status"] != STATUS_ACTIVE:
        return None
    if item.get("location") == "freezer":
        return item
    return transition_item(item_id, location="freezer")


def defrost_item(item_id: int) -> dict | None:
    item = get_item(item_id)
    if not item or item["status"] != STATUS_ACTIVE:
        return None
    if item.get("location") != "freezer":
        return item
    return transition_item(item_id, location="fridge")


def add_one(item_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM items WHERE id=? AND status=?", (item_id, STATUS_ACTIVE)).fetchone()
        if not row:
            return None
        if row["count"] >= 99:
            return None
        conn.execute("UPDATE items SET count=count+1,updated_at=? WHERE id=?", (now_iso(), row["id"]))
        return dict(conn.execute("SELECT * FROM items WHERE id=?", (row["id"],)).fetchone())


def stats() -> dict:
    current = today().replace(day=1)
    previous = (current - timedelta(days=1)).replace(day=1)
    following = (current.replace(day=28) + timedelta(days=4)).replace(day=1)
    with _connect() as conn:
        rows = conn.execute("SELECT outcome,created_at,name FROM item_events").fetchall()
    zone = local_zone()

    def event_date(row):
        try:
            value = datetime.fromisoformat(row["created_at"])
            if value.tzinfo is None:
                value = value.replace(tzinfo=zone)
            return value.astimezone(zone).date()
        except (TypeError, ValueError):
            return None

    def counts(start, end):
        selected = [r for r in rows if (event_date(r) is not None and start <= event_date(r) < end)]
        return {outcome: sum(r["outcome"] == outcome for r in selected) for outcome in ("eaten", "binned")}
    month_rows = [r for r in rows if event_date(r) is not None and current <= event_date(r) < following]
    names = {}
    for row in month_rows:
        if row["outcome"] == "binned":
            names[row["name"]] = names.get(row["name"], 0) + 1
    top = sorted(names.items(), key=lambda pair: (-pair[1], pair[0]))[:5]
    return {"this_month": counts(current, following),
            "last_month": counts(previous, current), "most_binned": [{"name": n, "count": c} for n, c in top]}


def finish_item(item_id: int, outcome: str) -> int | None:
    if outcome not in {"eaten", "binned"}:
        return None
    with _connect() as conn:
        row = conn.execute("SELECT * FROM items WHERE id = ? AND status = ?", (item_id, STATUS_ACTIVE)).fetchone()
        if not row:
            return None
        item = dict(row)
        cur = conn.execute(
            "INSERT INTO item_events (item_id, barcode, name, category, outcome, created_at) VALUES (?,?,?,?,?,?)",
            (item_id, item.get("barcode"), item["name"], item.get("category"), outcome, now_iso()),
        )
        if item["count"] <= 1:
            conn.execute("UPDATE items SET count=1,status=?,consumed_at=?,consumed_outcome=?,updated_at=? WHERE id=?",
                         (STATUS_CONSUMED, now_iso(), outcome, now_iso(), item_id))
        else:
            conn.execute("UPDATE items SET count=count-1,updated_at=? WHERE id=?", (now_iso(), item_id))
        return cur.lastrowid


def undo_event(event_id: int) -> bool:
    with _connect() as conn:
        event = conn.execute("SELECT * FROM item_events WHERE id=?", (event_id,)).fetchone()
        if not event:
            return False
        row = conn.execute("SELECT * FROM items WHERE id=?", (event["item_id"],)).fetchone()
        if row:
            item = dict(row)
            if item["status"] == STATUS_CONSUMED:
                conn.execute("UPDATE items SET status=?,count=1,consumed_at=NULL,consumed_outcome=NULL,updated_at=? WHERE id=?",
                             (STATUS_ACTIVE, now_iso(), item["id"]))
            else:
                conn.execute("UPDATE items SET count=count+1,updated_at=? WHERE id=?", (now_iso(), item["id"]))
        conn.execute("DELETE FROM item_events WHERE id=?", (event_id,))
        return True


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


def valid_barcode(barcode: str | None) -> bool:
    # Barcodes become filenames below, so only plain EAN/UPC digits qualify.
    return bool(barcode and re.fullmatch(r"\d{6,14}", barcode))


def product_photo_path(barcode: str) -> Path:
    if not valid_barcode(barcode):
        raise ValueError("Invalid barcode")
    return DATA_DIR / "product_photos" / f"{barcode}.jpg"


def product_image_path(barcode: str) -> Path:
    if not valid_barcode(barcode):
        raise ValueError("Invalid barcode")
    return DATA_DIR / "product_images" / f"{barcode}.jpg"


def save_product_image(barcode: str, image_bytes: bytes) -> None:
    path = product_image_path(barcode)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(image_bytes)


def save_product_photo(barcode: str, image_bytes: bytes) -> None:
    """Keyed by barcode rather than item, and deliberately kept when an item
    is deleted or consumed: the point is that the next scan of the same
    product reuses it."""
    path = product_photo_path(barcode)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(image_bytes)


def barcodes_missing_image() -> list[str]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT barcode FROM items WHERE barcode IS NOT NULL AND barcode != '' "
            "AND (image_url IS NULL OR image_url = '')"
        ).fetchall()
        return [r["barcode"] for r in rows]


def apply_barcode_image(barcode: str, image_url: str) -> None:
    """Fills in a missing image only, leaving any corrected name or brand
    (on the items or in the cache) untouched."""
    with _connect() as conn:
        conn.execute(
            "UPDATE items SET image_url = ? WHERE barcode = ? AND (image_url IS NULL OR image_url = '')",
            (image_url, barcode),
        )
        conn.execute(
            "UPDATE barcode_cache SET image_url = ? WHERE barcode = ? AND (image_url IS NULL OR image_url = '')",
            (image_url, barcode),
        )


def start_date_extraction(item_id: int, ocr_date: str | None = None, ocr_raw: str | None = None, ocr_candidates: list | None = None) -> None:
    """Marks an item as having a use-by date photo queued for background
    extraction. Called the moment the photo is saved, before the vision
    model has even been asked - see main.py's _extract_and_update.
    `ocr_date`/`ocr_raw` are what the browser's on-device OCR made of the
    same photo, if it got an answer in time."""
    update_item(
        item_id,
        has_date_photo=1,
        date_extraction_pending=1,
        date_ocr_date=ocr_date,
        date_ocr_raw=ocr_raw,
        date_ocr_candidates=json.dumps(ocr_candidates or ([{"date": ocr_date, "repaired": False}] if ocr_date else [])),
        date_confidence=None,
        date_candidates=None,
    )


def reconcile_readings(ocr_candidates, vision_candidates) -> tuple[str | None, str, list[str]]:
    """Combines the two independent date readings into (date, confidence,
    candidates). A disagreement gives no date at all: one of the two is
    wrong and a person can settle it from the photo, whereas a wrong date
    that's merely flagged would still drive the expiry notifications."""
    def normalized(values):
        if values is None: return []
        if isinstance(values, str): values = [{"date": values, "repaired": False}]
        elif values and isinstance(values[0], str): values = [{"date": value, "repaired": False} for value in values]
        return values
    ocr, vision = normalized(ocr_candidates), normalized(vision_candidates)
    if ocr and vision:
        if ocr[0]["date"] == vision[0]["date"]:
            repaired = ocr[0].get("repaired") or vision[0].get("repaired")
            return ocr[0]["date"], CONFIDENCE_CHECK if repaired else CONFIDENCE_CONFIRMED, []
        shared = set(c["date"] for c in ocr) & set(c["date"] for c in vision)
        if shared:
            chosen = min(shared, key=lambda d: (next(i for i,c in enumerate(ocr) if c["date"] == d) + next(i for i,c in enumerate(vision) if c["date"] == d), d))
            repaired = any(c["date"] == chosen and c.get("repaired") for c in ocr + vision)
            return chosen, CONFIDENCE_CHECK if repaired else CONFIDENCE_CONFIRMED, []
        return None, CONFIDENCE_CONFLICT, [ocr[0]["date"], vision[0]["date"]]
    if ocr or vision:
        return (ocr or vision)[0]["date"], CONFIDENCE_CHECK, []
    return None, CONFIDENCE_UNREAD, []


def finish_date_extraction(item_id: int, vision_candidates, raw: str, *, only_if_blank: bool = False) -> None:
    """Called once background extraction completes, to settle the date from
    the on-device reading (if any) and the vision model's. A read that found
    nothing at all leaves whatever date the item already had alone.

    `only_if_blank` (used when adding a brand new item with a photo
    attached) leaves a date typed in by hand before tapping "Add to pantry"
    alone - otherwise it could get silently clobbered once the background
    read finishes, with no form left open to notice or correct it. A date
    the on-device OCR filled in isn't typed, so it still gets checked.
    Rescanning an existing item leaves this False, since overwriting
    whatever date was already there (right or wrong) is the whole point of
    a rescan.
    """
    fields = {"date_extraction_pending": 0, "date_extraction_raw": raw}
    current = get_item(item_id) or {}
    ocr_date = current.get("date_ocr_date")
    typed_date = only_if_blank and current.get("expiry_date") and current.get("expiry_date") != ocr_date
    if not typed_date:
        ocr_candidates = json.loads(current.get("date_ocr_candidates") or "[]")
        if not ocr_candidates and ocr_date: ocr_candidates = [{"date": ocr_date, "repaired": False}]
        expiry_date, confidence, candidates = reconcile_readings(ocr_candidates, vision_candidates)
        fields["date_confidence"] = confidence
        fields["date_candidates"] = json.dumps(candidates) if candidates else None
        if expiry_date or confidence == CONFIDENCE_CONFLICT:
            fields["expiry_date"] = expiry_date
    update_item(item_id, **fields)


def mark_notified(item_id: int, bucket: str) -> None:
    update_item(item_id, last_notified_at=now_iso(), last_notified_bucket=bucket)


def get_cached_barcodes(barcodes: list[str]) -> dict[str, dict]:
    codes = sorted(set(code for code in barcodes if code))
    if not codes:
        return {}
    placeholders = ",".join("?" for _ in codes)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM barcode_cache WHERE barcode IN ({placeholders})",
            codes,
        ).fetchall()
    return {row["barcode"]: dict(row) for row in rows}


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
            "INSERT INTO barcode_cache (barcode, name, brand, quantity, category, image_url, cached_at, suggested_location) "
            "VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(barcode) DO UPDATE SET name = excluded.name, brand = excluded.brand, "
            "quantity = excluded.quantity, category = excluded.category, image_url = excluded.image_url, "
            "cached_at = excluded.cached_at, "
            "suggested_location = COALESCE(excluded.suggested_location, barcode_cache.suggested_location)",
            (
                barcode,
                product.get("name", ""),
                product.get("brand", ""),
                product.get("quantity", ""),
                product.get("category", ""),
                product.get("image_url", ""),
                now_iso(),
                product.get("suggested_location"),
            ),
        )


def update_cached_barcode(barcode: str, **fields) -> None:
    if not fields:
        return
    with _connect() as conn:
        conn.execute("UPDATE barcode_cache SET " + ",".join(f"{key}=?" for key in fields) + " WHERE barcode=?",
                     (*fields.values(), barcode))


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


def expire_old_date_photos() -> int:
    """Removes use-by photos for items consumed more than 30 days ago."""
    cutoff = today() - timedelta(days=30)
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id,consumed_at FROM items WHERE status=? AND consumed_at IS NOT NULL",
            (STATUS_CONSUMED,),
        ).fetchall()
        expired_ids = []
        for row in rows:
            try:
                consumed = datetime.fromisoformat(row["consumed_at"]).date()
            except (TypeError, ValueError):
                continue
            if consumed < cutoff:
                expired_ids.append(row["id"])
        for item_id in expired_ids:
            conn.execute("UPDATE items SET has_date_photo=0 WHERE id=?", (item_id,))
    for item_id in expired_ids:
        delete_date_photo(item_id)
    return len(expired_ids)
