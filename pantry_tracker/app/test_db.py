import json
import json
from datetime import date, timedelta

import db


def test_add_and_get_item():
    item_id = db.add_item(barcode="123", name="Milk", brand="Tesco", expiry_date="2026-12-01")
    item = db.get_item(item_id)
    assert item["name"] == "Milk"
    assert item["brand"] == "Tesco"
    assert item["status"] == db.STATUS_ACTIVE
    assert item["source"] == "manual"


def test_list_items_filters_by_status():
    active_id = db.add_item(barcode=None, name="Bread")
    consumed_id = db.add_item(barcode=None, name="Eggs")
    db.consume_item(consumed_id)

    active = db.list_items([db.STATUS_ACTIVE])
    assert [i["id"] for i in active] == [active_id]

    consumed = db.list_items([db.STATUS_CONSUMED])
    assert [i["id"] for i in consumed] == [consumed_id]


def test_list_items_orders_soonest_expiry_first_with_no_date_last():
    far = db.add_item(barcode=None, name="Far", expiry_date="2030-01-01")
    near = db.add_item(barcode=None, name="Near", expiry_date="2025-01-01")
    no_date = db.add_item(barcode=None, name="NoDate")

    ordered = [i["id"] for i in db.list_items()]
    assert ordered == [near, far, no_date]


def test_update_item():
    item_id = db.add_item(barcode=None, name="Yoghurt")
    db.update_item(item_id, name="Yoghurt (large)", quantity="1kg")
    item = db.get_item(item_id)
    assert item["name"] == "Yoghurt (large)"
    assert item["quantity"] == "1kg"


def test_delete_item():
    item_id = db.add_item(barcode=None, name="Soup")
    db.delete_item(item_id)
    assert db.get_item(item_id) is None


def test_save_and_delete_date_photo():
    item_id = db.add_item(barcode=None, name="Photo Item")
    db.save_date_photo(item_id, b"fake-bytes")
    path = db.date_photo_path(item_id)
    assert path.exists()
    assert path.read_bytes() == b"fake-bytes"

    db.delete_date_photo(item_id)
    assert not path.exists()


def test_delete_item_also_deletes_its_date_photo():
    item_id = db.add_item(barcode=None, name="Photo Item")
    db.save_date_photo(item_id, b"fake-bytes")
    db.delete_item(item_id)
    assert not db.date_photo_path(item_id).exists()


def test_start_and_finish_date_extraction():
    item_id = db.add_item(barcode=None, name="Soup")
    db.start_date_extraction(item_id)
    item = db.get_item(item_id)
    assert item["has_date_photo"] == 1
    assert item["date_extraction_pending"] == 1

    db.finish_date_extraction(item_id, "2026-12-01", "2026-12-01")
    item = db.get_item(item_id)
    assert item["date_extraction_pending"] == 0
    assert item["expiry_date"] == "2026-12-01"
    assert item["date_extraction_raw"] == "2026-12-01"


def test_finish_date_extraction_without_date_keeps_existing_expiry():
    item_id = db.add_item(barcode=None, name="Soup", expiry_date="2099-01-01")
    db.start_date_extraction(item_id)
    db.finish_date_extraction(item_id, None, "UNKNOWN")
    item = db.get_item(item_id)
    assert item["date_extraction_pending"] == 0
    assert item["expiry_date"] == "2099-01-01"
    assert item["date_extraction_raw"] == "UNKNOWN"


def test_finish_date_extraction_only_if_blank_does_not_overwrite_a_typed_in_date():
    item_id = db.add_item(barcode=None, name="Soup", expiry_date="2099-01-01")
    db.start_date_extraction(item_id)
    db.finish_date_extraction(item_id, "2026-12-01", "2026-12-01", only_if_blank=True)
    item = db.get_item(item_id)
    assert item["expiry_date"] == "2099-01-01"  # the typed-in value wins
    assert item["date_extraction_raw"] == "2026-12-01"


def test_finish_date_extraction_only_if_blank_fills_in_when_there_is_nothing_to_protect():
    item_id = db.add_item(barcode=None, name="Soup")
    db.start_date_extraction(item_id)
    db.finish_date_extraction(item_id, "2026-12-01", "2026-12-01", only_if_blank=True)
    assert db.get_item(item_id)["expiry_date"] == "2026-12-01"


def test_init_db_migrates_an_older_schema_missing_new_columns():
    import sqlite3

    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute("DROP TABLE items")
        conn.execute(
            """
            CREATE TABLE items (
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

    db.init_db()  # simulates an add-on upgrade over an existing, older database

    item_id = db.add_item(barcode=None, name="Migrated")
    db.start_date_extraction(item_id)
    item = db.get_item(item_id)
    assert item["has_date_photo"] == 1
    assert item["date_extraction_pending"] == 1


def test_mark_notified_sets_bucket_and_timestamp():
    item_id = db.add_item(barcode=None, name="Cheese")
    db.mark_notified(item_id, db.BUCKET_EXPIRING_SOON)
    item = db.get_item(item_id)
    assert item["last_notified_bucket"] == db.BUCKET_EXPIRING_SOON
    assert item["last_notified_at"] is not None


def test_bucket_for_unknown_when_no_date():
    assert db.bucket_for(None, notify_days_before=3) == db.BUCKET_UNKNOWN
    assert db.bucket_for("not-a-date", notify_days_before=3) == db.BUCKET_UNKNOWN


def test_bucket_for_expired_when_in_past():
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    assert db.bucket_for(yesterday, notify_days_before=3) == db.BUCKET_EXPIRED


def test_bucket_for_expiring_soon_within_threshold():
    soon = (date.today() + timedelta(days=2)).isoformat()
    assert db.bucket_for(soon, notify_days_before=3) == db.BUCKET_EXPIRING_SOON


def test_bucket_for_ok_beyond_threshold():
    later = (date.today() + timedelta(days=30)).isoformat()
    assert db.bucket_for(later, notify_days_before=3) == db.BUCKET_OK


def test_days_left_for():
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    assert db.days_left_for(tomorrow) == 1
    assert db.days_left_for(None) is None
    assert db.days_left_for("garbage") is None


def test_get_cached_barcode_missing_returns_none():
    assert db.get_cached_barcode("0000000000000") is None


def test_cache_barcode_roundtrip():
    product = {"name": "Beans", "brand": "Heinz", "quantity": "415g", "category": "Tinned", "image_url": "http://x/img.jpg"}
    db.cache_barcode("9999999999999", product)
    cached = db.get_cached_barcode("9999999999999")
    assert cached["name"] == "Beans"
    assert cached["brand"] == "Heinz"
    assert cached["quantity"] == "415g"
    assert cached["category"] == "Tinned"
    assert cached["image_url"] == "http://x/img.jpg"
    assert cached["cached_at"] is not None


def test_cache_barcode_overwrites_on_recache():
    db.cache_barcode("123", {"name": "Old Name", "brand": "", "quantity": "", "category": "", "image_url": ""})
    db.cache_barcode("123", {"name": "New Name", "brand": "", "quantity": "", "category": "", "image_url": ""})
    assert db.get_cached_barcode("123")["name"] == "New Name"


def test_state_roundtrip():
    assert db.get_state("missing") is None
    assert db.get_state("missing", "fallback") == "fallback"
    db.set_state("key", "value")
    assert db.get_state("key") == "value"
    db.set_state("key", "updated")
    assert db.get_state("key") == "updated"


def test_init_db_clears_stale_notify_service_override():
    # Left over from when notify_service was DB-overridable - a value
    # saved here (even blank) would otherwise go on shadowing the add-on's
    # Configuration tab forever, which is exactly the "notify service
    # keeps getting lost after an update" bug this cleans up.
    db.set_state("setting:notify_service", "notify.mobile_app_stale")
    db.init_db()
    assert db.get_state("setting:notify_service") is None


def test_reconcile_readings_all_four_outcomes():
    assert db.reconcile_readings("2027-01-01", "2027-01-01") == ("2027-01-01", "confirmed", [])
    assert db.reconcile_readings("2027-01-01", None) == ("2027-01-01", "check", [])
    assert db.reconcile_readings("2027-01-01", "2027-01-02") == (None, "conflict", ["2027-01-01", "2027-01-02"])
    assert db.reconcile_readings(None, None) == (None, "unread", [])


def test_conflict_clears_machine_date_and_keeps_candidates():
    item_id = db.add_item(barcode=None, name="Soup", expiry_date="2027-01-01")
    db.start_date_extraction(item_id, "2027-01-02", "02 Jan 27")
    db.finish_date_extraction(item_id, "2027-01-01", "01 Jan 27")
    item = db.get_item(item_id)
    assert item["expiry_date"] is None
    assert item["date_confidence"] == "conflict"
    assert json.loads(item["date_candidates"]) == ["2027-01-02", "2027-01-01"]


def test_typed_date_is_preserved_without_confidence():
    item_id = db.add_item(barcode=None, name="Soup", expiry_date="2028-02-03")
    db.start_date_extraction(item_id, "2027-01-01", "01 Jan 27")
    db.finish_date_extraction(item_id, "2027-01-02", "02 Jan 27", only_if_blank=True)
    item = db.get_item(item_id)
    assert item["expiry_date"] == "2028-02-03"
    assert item["date_confidence"] is None


def test_rescan_replaces_existing_date_and_start_resets_confidence():
    item_id = db.add_item(barcode=None, name="Soup", expiry_date="2027-01-01")
    db.update_item(item_id, date_confidence="confirmed", date_candidates='["2027-01-01"]')
    db.start_date_extraction(item_id, "2027-02-01", "01 Feb 27")
    started = db.get_item(item_id)
    assert started["date_confidence"] is None
    assert started["date_candidates"] is None
    db.finish_date_extraction(item_id, "2027-02-01", "01 Feb 27")
    assert db.get_item(item_id)["expiry_date"] == "2027-02-01"


def test_unread_result_keeps_existing_date():
    item_id = db.add_item(barcode=None, name="Soup", expiry_date="2027-01-01")
    db.start_date_extraction(item_id)
    db.finish_date_extraction(item_id, None, "UNKNOWN")
    item = db.get_item(item_id)
    assert item["expiry_date"] == "2027-01-01"
    assert item["date_confidence"] == "unread"
