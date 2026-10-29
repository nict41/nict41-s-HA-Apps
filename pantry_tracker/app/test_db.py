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
