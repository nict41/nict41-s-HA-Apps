from io import BytesIO
import asyncio
import threading
import time
import pytest

import db
import main
import settings
from fastapi.testclient import TestClient
from PIL import Image

client = TestClient(main.app)


def _jpeg_bytes() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (1600, 1200), "white").save(buf, format="JPEG")
    return buf.getvalue()


def test_index_page_loads():
    res = client.get("/")
    assert res.status_code == 200
    assert "Pantry Tracker" in res.text


def test_scan_returns_decoded_barcode(monkeypatch):
    monkeypatch.setattr(main.barcode, "decode_barcode", lambda image_bytes: "5000157024671")
    res = client.post("/api/scan", files={"photo": ("p.jpg", b"fake-bytes", "image/jpeg")})
    assert res.status_code == 200
    assert res.json() == {"barcode": "5000157024671"}


def test_scan_returns_none_when_undecodable(monkeypatch):
    monkeypatch.setattr(main.barcode, "decode_barcode", lambda image_bytes: None)
    res = client.post("/api/scan", files={"photo": ("p.jpg", b"fake-bytes", "image/jpeg")})
    assert res.json() == {"barcode": None}


def test_lookup_product_found(monkeypatch):
    monkeypatch.setattr(
        main.lookup,
        "lookup_barcode",
        lambda code: {"name": "Milk", "brand": "Tesco", "quantity": "1L", "category": "Dairy", "categories_tags": ["en:dairies"], "image_url": ""},
    )
    res = client.get("/api/lookup/123")
    assert res.json()["found"] is True
    assert res.json()["name"] == "Milk"
    assert res.json()["cached"] is False
    assert res.json()["suggested_location"] == "fridge"


def test_lookup_product_caches_and_skips_network_on_repeat_scan(monkeypatch):
    calls = []
    monkeypatch.setattr(
        main.lookup,
        "lookup_barcode",
        lambda code: calls.append(code) or {
            "name": "Beans", "brand": "Heinz", "quantity": "415g", "category": "Tinned",
            "categories_tags": ["en:canned-foods"], "image_url": "",
        },
    )

    first = client.get("/api/lookup/9999")
    assert first.json()["found"] is True
    assert first.json()["cached"] is False
    assert calls == ["9999"]

    # A second scan of the same barcode must not call lookup_barcode again -
    # prove the cache path by making a repeat network call fail loudly.
    def _fail_if_called(code):
        raise AssertionError("network hit on a cached barcode")

    monkeypatch.setattr(main.lookup, "lookup_barcode", _fail_if_called)

    second = client.get("/api/lookup/9999")
    assert second.json() == {
        "found": True,
        "cached": True,
        "name": "Beans",
        "brand": "Heinz",
        "quantity": "415g",
        "category": "Tinned",
        "image_url": "",
        "display_image_url": "",
        "suggested_location": "cupboard",
    }
    assert calls == ["9999"]


def test_lookup_product_not_found(monkeypatch):
    monkeypatch.setattr(main.lookup, "lookup_barcode", lambda code: None)
    res = client.get("/api/lookup/123")
    assert res.json() == {"found": False, "display_image_url": ""}


def test_cached_legacy_lookup_computes_suggestion_without_network(monkeypatch):
    db.cache_barcode("legacy", {"name": "Plain flour", "category": "", "image_url": ""})
    db.update_cached_barcode("legacy", suggested_location=None)
    monkeypatch.setattr(main.lookup, "lookup_barcode", lambda code: pytest.fail("network called"))
    result = client.get("/api/lookup/legacy").json()
    assert result["cached"] is True
    assert result["suggested_location"] == "cupboard"


def test_product_photo_roundtrip_is_shrunk_and_wins_over_open_food_facts():
    db.cache_barcode("5000000000001", {"name": "Tin", "brand": "", "quantity": "", "category": "", "image_url": "https://x/off.jpg"})
    assert client.get("/api/lookup/5000000000001").json()["display_image_url"] == "https://x/off.jpg"

    res = client.post("/api/barcodes/5000000000001/photo", files={"photo": ("p.jpg", _jpeg_bytes(), "image/jpeg")})
    assert res.status_code == 200
    url = res.json()["display_image_url"]
    assert url.startswith("api/barcodes/5000000000001/photo")
    assert client.get("/api/lookup/5000000000001").json()["display_image_url"] == url

    stored = client.get("/api/barcodes/5000000000001/photo")
    assert stored.status_code == 200
    assert max(Image.open(BytesIO(stored.content)).size) == 800


def test_product_photo_shows_for_a_barcode_open_food_facts_does_not_know(monkeypatch):
    monkeypatch.setattr(main.lookup, "lookup_barcode", lambda code: None)
    client.post("/api/barcodes/5000000000002/photo", files={"photo": ("p.jpg", _jpeg_bytes(), "image/jpeg")})
    res = client.get("/api/lookup/5000000000002").json()
    assert res["found"] is False
    assert res["display_image_url"].startswith("api/barcodes/5000000000002/photo")


def test_product_photo_survives_item_deletion_and_shows_on_items():
    client.post("/api/barcodes/5000000000003/photo", files={"photo": ("p.jpg", _jpeg_bytes(), "image/jpeg")})
    item = client.post("/api/items", data={"barcode": "5000000000003", "name": "Beans"}).json()
    assert item["display_image_url"].startswith("api/barcodes/5000000000003/photo")

    client.post(f"/api/items/{item['id']}/delete")
    assert client.get("/api/barcodes/5000000000003/photo").status_code == 200


def test_product_photo_rejects_bad_barcodes_and_non_images():
    bad = client.post("/api/barcodes/abc/photo", files={"photo": ("p.jpg", _jpeg_bytes(), "image/jpeg")})
    assert bad.status_code == 400
    not_image = client.post("/api/barcodes/5000000000004/photo", files={"photo": ("p.jpg", b"nope", "image/jpeg")})
    assert not_image.status_code == 400
    assert client.get("/api/barcodes/5000000000004/photo").status_code == 404


def test_image_backfill_uses_cache_then_open_food_facts_and_keeps_corrections(monkeypatch):
    monkeypatch.setattr(main, "_BACKFILL_LOOKUP_DELAY_SECONDS", 0)
    db.cache_barcode("6000000000001", {"name": "Corrected", "brand": "", "quantity": "", "category": "", "image_url": "https://x/cached.jpg"})
    db.cache_barcode("6000000000002", {"name": "Corrected Too", "brand": "", "quantity": "", "category": "", "image_url": ""})
    from_cache = db.add_item(barcode="6000000000001", name="Corrected")
    from_lookup = db.add_item(barcode="6000000000002", name="Corrected Too")
    monkeypatch.setattr(main.lookup, "lookup_barcode", lambda code: {"name": "Garbled", "image_url": "https://x/off.jpg"})

    main.run_image_backfill()

    assert db.get_item(from_cache)["image_url"] == "https://x/cached.jpg"
    assert db.get_item(from_lookup)["image_url"] == "https://x/off.jpg"
    assert db.get_item(from_lookup)["name"] == "Corrected Too"
    assert db.get_cached_barcode("6000000000002")["name"] == "Corrected Too"
    assert db.get_cached_barcode("6000000000002")["image_url"] == "https://x/off.jpg"


def test_image_backfill_retries_after_a_failed_lookup_then_stops(monkeypatch):
    monkeypatch.setattr(main, "_BACKFILL_LOOKUP_DELAY_SECONDS", 0)
    item_id = db.add_item(barcode="6000000000003", name="Soup")
    monkeypatch.setattr(main.lookup, "lookup_barcode", lambda code: None)
    main.run_image_backfill()
    assert db.get_state(main._IMAGE_BACKFILL_STATE_KEY) is None

    monkeypatch.setattr(main.lookup, "lookup_barcode", lambda code: {"name": "Soup", "image_url": "https://x/soup.jpg"})
    main.run_image_backfill()
    assert db.get_item(item_id)["image_url"] == "https://x/soup.jpg"
    assert db.get_state(main._IMAGE_BACKFILL_STATE_KEY) is not None


def test_add_item_requires_name():
    res = client.post("/api/items", data={"name": "   "})
    assert res.status_code == 400


def test_add_list_and_item_lifecycle():
    res = client.post(
        "/api/items",
        data={"barcode": "123", "name": "Milk", "brand": "Tesco", "expiry_date": "2099-01-01", "source": "barcode"},
    )
    assert res.status_code == 200
    item = res.json()
    assert item["name"] == "Milk"
    assert item["bucket"] == db.BUCKET_OK

    res = client.get("/api/items")
    items = res.json()["items"]
    assert any(i["id"] == item["id"] for i in items)

    res = client.post(f"/api/items/{item['id']}", data={"name": "Milk (2%)", "expiry_date": "2099-01-01"})
    assert res.status_code == 200
    assert res.json()["name"] == "Milk (2%)"

    res = client.post(f"/api/items/{item['id']}/finish", data={"outcome": "eaten"})
    assert res.status_code == 200
    assert db.get_item(item["id"])["status"] == db.STATUS_CONSUMED

    res = client.post(f"/api/items/{item['id']}/delete")
    assert res.status_code == 200
    assert db.get_item(item["id"]) is None


def test_only_nonblank_add_remembers_location(monkeypatch):
    code = "memory-test"
    db.cache_barcode(code, {"name": "Soup", "category": "", "image_url": ""})
    added = client.post("/api/items", data={"barcode": code, "name": "Soup", "location": "cupboard"}).json()
    assert db.get_cached_barcode(code)["location"] == "cupboard"
    blank = client.post("/api/items", data={"barcode": code, "name": "Soup"}).json()
    assert db.get_cached_barcode(code)["location"] == "cupboard"
    for data in ({"name": "Soup", "location": "fridge"}, {"name": "Soup", "location": "freezer"}):
        client.post(f"/api/items/{added['id']}", data=data)
        assert db.get_cached_barcode(code)["location"] == "cupboard"
    client.post(f"/api/items/{added['id']}/storage", data={"action": "freeze"})
    assert db.get_cached_barcode(code)["location"] == "cupboard"
    client.post(f"/api/items/{added['id']}/storage", data={"action": "defrost"})
    assert db.get_cached_barcode(code)["location"] == "cupboard"
    client.post(f"/api/items/{added['id']}/storage/undo", data={"location": "fridge", "defrosted_at": ""})
    assert db.get_cached_barcode(code)["location"] == "cupboard"
    client.post(f"/api/items/{blank['id']}/opened", data={"days": 4})
    assert db.get_cached_barcode(code)["location"] == "cupboard"


def test_freeze_defrost_routes_and_undo_restore_exact_storage_state(monkeypatch):
    syncs = []
    monkeypatch.setattr(main.ha_sync, "request_sync", lambda: syncs.append(True))
    item_id = db.add_item(barcode="5000000000123", name="Soup", location="cupboard")
    db.update_item(item_id, defrosted_at="2026-01-02")
    db.cache_barcode("5000000000123", {"name": "Soup", "location": "fridge"})
    cached = db.get_cached_barcode("5000000000123")

    frozen = client.post(f"/api/items/{item_id}/storage", data={"action": "freeze"})
    assert frozen.status_code == 200
    assert frozen.json()["location"] == "freezer"
    assert frozen.json()["undo"] == {"location": "cupboard", "defrosted_at": "2026-01-02"}
    assert db.get_cached_barcode("5000000000123") == cached
    restored_freezer = client.post(
        f"/api/items/{item_id}/storage/undo", data=frozen.json()["undo"]
    )
    assert restored_freezer.status_code == 200
    assert (restored_freezer.json()["location"], restored_freezer.json()["defrosted_at"]) == (
        "cupboard", "2026-01-02"
    )

    client.post(f"/api/items/{item_id}/storage", data={"action": "freeze"})
    defrosted = client.post(f"/api/items/{item_id}/storage", data={"action": "defrost"})
    assert defrosted.status_code == 200
    assert defrosted.json()["location"] == "fridge"
    assert defrosted.json()["defrosted_at"] == db.today().isoformat()
    assert defrosted.json()["undo"] == {"location": "freezer", "defrosted_at": ""}
    undone = client.post(f"/api/items/{item_id}/storage/undo", data=defrosted.json()["undo"])
    assert undone.status_code == 200
    assert (undone.json()["location"], undone.json()["defrosted_at"]) == ("freezer", None)
    restored = client.post(
        f"/api/items/{item_id}/storage/undo",
        data={"location": "cupboard", "defrosted_at": "2026-01-02"},
    )
    assert restored.status_code == 200
    assert (restored.json()["location"], restored.json()["defrosted_at"]) == ("cupboard", "2026-01-02")
    assert len(syncs) == 6


def test_storage_routes_reject_bad_action_missing_and_consumed_items():
    item_id = db.add_item(barcode=None, name="Milk")
    assert client.post(f"/api/items/{item_id}/storage", data={"action": "open"}).status_code == 400
    assert client.post("/api/items/99999/storage", data={"action": "freeze"}).status_code == 404
    db.finish_item(item_id, "eaten")
    assert client.post(f"/api/items/{item_id}/storage", data={"action": "freeze"}).status_code == 404
    assert client.post(
        f"/api/items/{item_id}/storage/undo", data={"location": "fridge", "defrosted_at": ""}
    ).status_code == 404


def test_storage_undo_validates_location_and_iso_date():
    item_id = db.add_item(barcode=None, name="Milk")
    assert client.post(f"/api/items/{item_id}/storage/undo", data={"location": "garage", "defrosted_at": ""}).status_code == 400
    assert client.post(f"/api/items/{item_id}/storage/undo", data={"location": "fridge", "defrosted_at": "tomorrow"}).status_code == 400

def test_add_validates_count_location_and_opened_days():
    assert client.post("/api/items", data={"name": "Soup", "count": 100}).status_code == 400
    assert client.post("/api/items", data={"name": "Soup", "location": "garage"}).status_code == 400
    assert client.post("/api/items", data={"name": "Soup", "use_within_days": 366}).status_code == 400


def test_location_opened_and_add_one_routes(monkeypatch):
    item_id = db.add_item(barcode="1234567", name="Soup", count=1)
    assert client.post(f"/api/items/{item_id}/opened", data={"days": 3}).status_code == 200
    assert db.get_item(item_id)["use_within_days"] == 3
    frozen = client.post(f"/api/items/{item_id}", data={"name": "Soup", "location": "freezer"})
    assert frozen.json()["bucket"] == db.BUCKET_FROZEN
    thawed = client.post(f"/api/items/{item_id}", data={"name": "Soup", "location": "fridge"})
    assert thawed.json()["defrosted_at"] == db.today().isoformat()
    newest_id = db.add_item(barcode="1234567", name="Different date", count=4)
    result = client.post(f"/api/items/{item_id}/add-one")
    assert result.json()["count"] == 2
    assert db.get_item(newest_id)["count"] == 4
    full_id = db.add_item(barcode=None, name="Full", count=99)
    assert client.post(f"/api/items/{full_id}/add-one").status_code == 400


def test_republish_only_syncs_after_404(monkeypatch):
    calls = []
    monkeypatch.setattr(main.ha_sync, "summary_state_exists", lambda: False)
    monkeypatch.setattr(main.ha_sync, "sync", lambda items: calls.append(items))
    monkeypatch.setattr(main.ha_sync, "notify_summary", lambda items: (_ for _ in ()).throw(AssertionError("notifier called")))
    main.run_republish_check()
    assert len(calls) == 1
    monkeypatch.setattr(main.ha_sync, "summary_state_exists", lambda: None)
    main.run_republish_check()
    assert len(calls) == 1


def test_tidy_migration_runs_once(monkeypatch):
    item_id = db.add_item(barcode=None, name="NATURAL SEA SALT", brand="BRAND NAME")
    main.tidy_existing_data()
    assert db.get_item(item_id)["name"] == "Natural Sea Salt"
    db.update_item(item_id, name="Hand Edited")
    main.tidy_existing_data()
    assert db.get_item(item_id)["name"] == "Hand Edited"


def test_tidy_migration_preserves_null_brand():
    item_id = db.add_item(barcode=None, name="NATURAL SEA SALT", brand=None)
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO barcode_cache (barcode, brand, cached_at) VALUES (?,?,?)",
            ("123456789", None, db.now_iso()),
        )
    main.tidy_existing_data()
    assert db.get_item(item_id)["brand"] is None
    assert db.get_cached_barcode("123456789")["brand"] is None


def test_daily_summary_job_and_midnight_jobs_use_local_time_grace(monkeypatch, capsys):
    class FakeScheduler:
        running = False

        def __init__(self):
            self.jobs = []
            self.reschedules = []

        def add_job(self, *args, **kwargs):
            self.jobs.append((args, kwargs))

        def start(self):
            self.running = True

        def shutdown(self, wait=False):
            self.running = False

        def reschedule_job(self, *args, **kwargs):
            self.reschedules.append((args, kwargs))

    scheduler = FakeScheduler()
    monkeypatch.setattr(main, "scheduler", scheduler)
    monkeypatch.setattr(main, "tidy_existing_data", lambda: None)
    monkeypatch.setattr(main.ha_sync, "configured", lambda: False)
    monkeypatch.setenv("TZ", "Europe/London")
    settings.set_many({"notify_hour": 6})

    async def run_lifespan():
        async with main.lifespan(main.app):
            pass

    asyncio.run(run_lifespan())
    daily = next(job for job in scheduler.jobs if job[1].get("id") == main._NOTIFY_JOB_ID)
    midnight = next(job for job in scheduler.jobs if job[1].get("id") == main._SYNC_JOB_ID)
    assert midnight[1]["hour"] == 0
    assert midnight[1]["minute"] == 5
    assert midnight[1]["misfire_grace_time"] == 3600
    assert midnight[1]["coalesce"] is True
    republish = next(job for job in scheduler.jobs if job[1].get("id") == main._REPUBLISH_JOB_ID)
    assert republish[1]["minutes"] == 5
    assert any(job[1].get("id") == main._RETENTION_JOB_ID for job in scheduler.jobs)
    assert daily[1]["hour"] == 6
    assert daily[1]["misfire_grace_time"] == 3600
    assert daily[1]["coalesce"] is True
    assert "Daily summary at 06:00 Europe/London" in capsys.readouterr().out
    scheduler.running = True
    settings.set_many({"notify_hour": 9})
    main._reschedule_notify_job()
    assert scheduler.reschedules[-1][1]["hour"] == 9
    assert "Daily summary at 09:00 Europe/London" in capsys.readouterr().out


def test_edit_missing_item_returns_404():
    res = client.post("/api/items/999999", data={"name": "Ghost"})
    assert res.status_code == 404


def test_add_item_with_barcode_refreshes_cached_product():
    # A garbled/wrong Open Food Facts entry corrected before tapping "Add
    # to pantry" should fix the cache too, not just this one item.
    db.cache_barcode("4000000000000", {"name": "Garbled Name", "brand": "", "quantity": "", "category": "", "image_url": ""})
    client.post(
        "/api/items",
        data={"barcode": "4000000000000", "name": "Corrected Name", "brand": "Fixed Brand", "source": "barcode"},
    )
    cached = db.get_cached_barcode("4000000000000")
    assert cached["name"] == "Corrected Name"
    assert cached["brand"] == "Fixed Brand"


def test_edit_item_with_barcode_refreshes_cached_product():
    db.cache_barcode("4000000000001", {"name": "Garbled Name", "brand": "", "quantity": "", "category": "", "image_url": ""})
    created = client.post(
        "/api/items", data={"barcode": "4000000000001", "name": "Garbled Name", "source": "barcode"}
    ).json()

    client.post(f"/api/items/{created['id']}", data={"name": "Fixed After The Fact"})

    cached = db.get_cached_barcode("4000000000001")
    assert cached["name"] == "Fixed After The Fact"


def test_add_item_without_barcode_does_not_touch_cache():
    client.post("/api/items", data={"name": "No Barcode Item"})
    assert db.get_cached_barcode("") is None


def test_get_single_item():
    created = client.post("/api/items", data={"name": "Solo"}).json()
    res = client.get(f"/api/items/{created['id']}")
    assert res.status_code == 200
    assert res.json()["name"] == "Solo"


def test_get_single_item_missing_returns_404():
    res = client.get("/api/items/999999")
    assert res.status_code == 404


def test_add_item_with_date_photo_extracts_in_background(monkeypatch):
    monkeypatch.setattr(
        main.vision, "extract_use_by_date", lambda image_bytes: {"date": "2026-12-01", "raw": "2026-12-01"}
    )
    res = client.post(
        "/api/items",
        data={"name": "Yoghurt"},
        files={"date_photo": ("use-by.jpg", b"fake-bytes", "image/jpeg")},
    )
    assert res.status_code == 200
    item = res.json()
    assert item["has_date_photo"] is True
    # The response is built before the background task runs, so it still
    # reflects "pending" - that's the point: "Add to pantry" doesn't wait.
    assert item["date_extraction_pending"] is True

    # TestClient drives a request's background tasks to completion before
    # returning, so by now extraction has already happened for real.
    stored = db.get_item(item["id"])
    assert stored["date_extraction_pending"] == 0
    assert stored["expiry_date"] == "2026-12-01"


def test_add_item_with_date_photo_does_not_override_a_typed_in_date(monkeypatch):
    monkeypatch.setattr(
        main.vision, "extract_use_by_date", lambda image_bytes: {"date": "2026-12-01", "raw": "2026-12-01"}
    )
    res = client.post(
        "/api/items",
        data={"name": "Yoghurt", "expiry_date": "2099-01-01"},
        files={"date_photo": ("use-by.jpg", b"fake-bytes", "image/jpeg")},
    )
    item_id = res.json()["id"]
    stored = db.get_item(item_id)
    assert stored["expiry_date"] == "2099-01-01"  # the typed-in value wins
    assert stored["date_extraction_raw"] == "2026-12-01"


def test_upload_date_photo_for_existing_item(monkeypatch):
    monkeypatch.setattr(main.vision, "extract_use_by_date", lambda image_bytes: {"date": None, "raw": "UNKNOWN"})
    res = client.post("/api/items", data={"name": "Soup", "expiry_date": "2099-01-01"})
    item_id = res.json()["id"]

    res = client.post(f"/api/items/{item_id}/date-photo", files={"photo": ("p.jpg", b"fake-bytes", "image/jpeg")})
    assert res.status_code == 200
    assert res.json()["has_date_photo"] is True

    # No date found - the existing expiry_date is left alone, not cleared.
    stored = db.get_item(item_id)
    assert stored["expiry_date"] == "2099-01-01"
    assert stored["date_extraction_raw"] == "UNKNOWN"


def test_upload_date_photo_missing_item_returns_404():
    res = client.post("/api/items/999999/date-photo", files={"photo": ("p.jpg", b"fake-bytes", "image/jpeg")})
    assert res.status_code == 404


def test_date_photo_roundtrip_and_404_when_absent():
    res = client.post("/api/items", data={"name": "Tin"})
    item_id = res.json()["id"]

    assert client.get(f"/api/items/{item_id}/date-photo").status_code == 404

    client.post(f"/api/items/{item_id}/date-photo", files={"photo": ("p.jpg", b"fake-bytes", "image/jpeg")})
    fetched = client.get(f"/api/items/{item_id}/date-photo")
    assert fetched.status_code == 200
    assert fetched.content == b"fake-bytes"


def test_delete_item_removes_its_stored_date_photo():
    res = client.post("/api/items", data={"name": "ToDelete"})
    item_id = res.json()["id"]
    client.post(f"/api/items/{item_id}/date-photo", files={"photo": ("p.jpg", b"fake-bytes", "image/jpeg")})
    assert db.date_photo_path(item_id).exists()

    client.post(f"/api/items/{item_id}/delete")
    assert not db.date_photo_path(item_id).exists()


def test_settings_roundtrip():
    res = client.post(
        "/api/settings",
        data={
            "notify_days_before": 5,
            "notify_repeat_hours": 48,
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["notify_days_before"] == 5
    assert body["notify_repeat_hours"] == 48

    res = client.get("/api/settings")
    assert res.json()["notify_days_before"] == 5


def test_settings_reflects_notify_service_from_environment_not_the_database(monkeypatch):
    # notify_service is add-on-configuration-only - posting one here (even
    # if a client still sent it) must never store or reflect it; only the
    # environment (what the add-on's Configuration tab sets) counts.
    monkeypatch.setenv("NOTIFY_SERVICE", "notify.mobile_app_phone")
    res = client.post(
        "/api/settings",
        data={
            "notify_days_before": 5,
            "notify_repeat_hours": 48,
            "notify_service": "notify.mobile_app_a_completely_different_one",
        },
    )
    assert res.status_code == 200
    assert res.json()["notify_service"] == "notify.mobile_app_phone"

    monkeypatch.setenv("NOTIFY_SERVICE", "notify.mobile_app_other")
    assert client.get("/api/settings").json()["notify_service"] == "notify.mobile_app_other"


def test_check_now_runs_without_ha_configured():
    res = client.post("/api/check-now")
    assert res.status_code == 200
    assert res.json() == {"result": "not_configured"}


def test_export_returns_json_attachment():
    client.post("/api/items", data={"name": "Export Me"})
    res = client.get("/export")
    assert res.status_code == 200
    assert "attachment" in res.headers["content-disposition"]
    assert any(i["name"] == "Export Me" for i in res.json()["items"])


def test_parse_date_endpoint(monkeypatch):
    monkeypatch.setattr(main.vision, "date_candidates", lambda text, lenient=False: [{"date": "2027-03-01", "repaired": False, "score": 5}] if text == "01 03 27" else [])
    assert client.post("/api/parse-date", data={"text": "01 03 27"}).json()["date"] == "2027-03-01"
    assert client.post("/api/parse-date", data={"text": "nothing"}).json()["date"] is None


def test_ocr_fields_are_stored_with_date_photo(monkeypatch):
    monkeypatch.setattr(main.vision, "extract_use_by_date", lambda image_bytes: {"date": "2027-01-02", "raw": "02 Jan 27"})
    item = client.post(
        "/api/items",
        data={"name": "Soup", "ocr_date": "2027-01-01", "ocr_raw": "01 Jan 27"},
        files={"date_photo": ("use-by.jpg", b"photo", "image/jpeg")},
    ).json()
    stored = db.get_item(item["id"])
    assert stored["date_ocr_date"] == "2027-01-01"
    assert stored["date_ocr_raw"] == "01 Jan 27"
    assert stored["date_confidence"] == "conflict"
    assert client.get(f"/api/items/{item['id']}").json()["date_candidates"] == ["2027-01-01", "2027-01-02"]
    assert stored["expiry_date"] is None


def test_invalid_ocr_date_is_ignored():
    item = client.post(
        "/api/items",
        data={"name": "Soup", "ocr_date": "not-a-date", "ocr_raw": "untrusted"},
        files={"date_photo": ("use-by.jpg", b"photo", "image/jpeg")},
    ).json()
    assert db.get_item(item["id"])["date_ocr_date"] is None
    assert db.get_item(item["id"])["date_ocr_raw"] == "untrusted"


def test_non_calendar_iso_date_form_is_ignored():
    item = client.post(
        "/api/items",
        data={"name": "Soup", "ocr_date": "2027-W01-1"},
        files={"date_photo": ("use-by.jpg", b"photo", "image/jpeg")},
    ).json()
    assert db.get_item(item["id"])["date_ocr_date"] is None


def test_edit_changed_date_clears_confidence_but_unchanged_date_keeps_it():
    item_id = db.add_item(barcode=None, name="Soup", expiry_date="2027-01-01")
    db.update_item(item_id, date_confidence="confirmed", date_candidates='["2027-01-01"]')
    client.post(f"/api/items/{item_id}", data={"name": "Soup", "expiry_date": "2027-01-01"})
    assert db.get_item(item_id)["date_confidence"] == "confirmed"
    client.post(f"/api/items/{item_id}", data={"name": "Soup", "expiry_date": "2027-01-02"})
    assert db.get_item(item_id)["date_confidence"] is None
    assert db.get_item(item_id)["date_candidates"] is None


def test_item_candidates_are_exposed_as_a_list():
    item_id = db.add_item(barcode=None, name="Soup")
    db.update_item(item_id, date_confidence="conflict", date_candidates='["2027-01-01", "2027-01-02"]')
    assert client.get(f"/api/items/{item_id}").json()["date_candidates"] == ["2027-01-01", "2027-01-02"]


def test_static_mount_tolerates_missing_ocr_directory_and_sets_immutable_header(tmp_path, monkeypatch):
    static_mount = next(route for route in main.app.routes if getattr(route, "name", None) == "static")
    monkeypatch.setattr(static_mount.app, "all_directories", [str(tmp_path)])
    assert client.get("/static/ocr/absent.wasm").status_code == 404
    ocr_dir = tmp_path / "ocr"
    ocr_dir.mkdir()
    (ocr_dir / "sample.wasm").write_bytes(b"wasm")
    response = client.get("/static/ocr/sample.wasm")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    (tmp_path / "worker.js").write_text("// worker")
    assert client.get("/static/worker.js").headers["cache-control"] == "no-cache"


def test_tapping_the_current_date_settles_its_confidence():
    item_id = db.add_item(barcode=None, name="Soup", expiry_date="2027-01-01")
    db.update_item(item_id, date_confidence="check")
    client.post(f"/api/items/{item_id}", data={"name": "Soup", "expiry_date": "2027-01-01"})
    assert db.get_item(item_id)["date_confidence"] == "check"
    client.post(f"/api/items/{item_id}", data={"name": "Soup", "expiry_date": "2027-01-01", "date_chosen": "1"})
    assert db.get_item(item_id)["date_confidence"] is None


def test_client_timing_rejects_unknown_fields_and_paths(capsys):
    response = client.post("/api/client-timing", data={
        "path": "live", "open_ms": "12", "read_slow": "1", "tick_ms": "130",
        "secret": "never-log",
    })
    assert response.status_code == 200
    output = capsys.readouterr().out
    assert "read_slow=1" in output
    assert "tick_ms=130" in output
    assert "secret" not in output
    response = client.post("/api/client-timing", data={"path": "unknown", "open_ms": "12"})
    assert response.status_code == 400


def test_vision_read_retries_once_on_timeout_only(monkeypatch):
    item_id = db.add_item(barcode=None, name="Retry")
    db.save_date_photo(item_id, b"photo")
    calls = []
    def extract(_photo):
        calls.append(1)
        if len(calls) == 1:
            return {"date": None, "candidates": [], "raw": "Vision request failed: slow model", "retryable": True}
        return {"date": None, "candidates": [], "raw": "UNKNOWN"}
    monkeypatch.setattr(main.vision, "extract_use_by_date", extract)
    main._extract_and_update(item_id)
    assert len(calls) == 2


def test_vision_read_does_not_retry_model_answer(monkeypatch):
    item_id = db.add_item(barcode=None, name="No retry")
    db.save_date_photo(item_id, b"photo")
    calls = []
    monkeypatch.setattr(main.vision, "extract_use_by_date", lambda _photo: calls.append(1) or {"date": None, "candidates": [], "raw": "UNKNOWN"})
    main._extract_and_update(item_id)
    assert len(calls) == 1


def test_vision_reads_never_overlap(monkeypatch):
    active = 0
    maximum = 0
    guard = threading.Lock()
    def extract(_photo):
        nonlocal active, maximum
        with guard:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.04)
        with guard:
            active -= 1
        return {"date": None, "candidates": [], "raw": "UNKNOWN"}
    monkeypatch.setattr(main.vision, "extract_use_by_date", extract)
    ids = []
    for name in ("One", "Two"):
        item_id = db.add_item(barcode=None, name=name)
        db.save_date_photo(item_id, b"photo")
        ids.append(item_id)
    threads = [threading.Thread(target=main._extract_and_update, args=(item_id,)) for item_id in ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert maximum == 1


def test_startup_requeue_selects_pending_items_with_photos(monkeypatch):
    selected = db.add_item(barcode=None, name="Selected")
    db.start_date_extraction(selected)
    db.save_date_photo(selected, b"photo")
    missing_photo = db.add_item(barcode=None, name="No photo")
    db.start_date_extraction(missing_photo)
    not_pending = db.add_item(barcode=None, name="Not pending")
    db.save_date_photo(not_pending, b"photo")
    pending_with_photo = db.add_item(barcode=None, name="Pending photo")
    db.update_item(pending_with_photo, date_extraction_pending=1)
    db.save_date_photo(pending_with_photo, b"photo")
    queued = []
    monkeypatch.setattr(main, "_extract_and_update", lambda item_id, **kwargs: queued.append(item_id))
    main.run_pending_date_extractions()
    assert queued == [selected, pending_with_photo]


def test_restart_requeue_keeps_a_typed_date(monkeypatch):
    item_id = db.add_item(barcode=None, name="Soup", expiry_date="2099-01-01")
    db.save_date_photo(item_id, b"photo")
    db.start_date_extraction(item_id)
    monkeypatch.setattr(main.vision, "extract_use_by_date", lambda image_bytes: {"date": "2027-01-02", "candidates": [{"date": "2027-01-02", "repaired": False}], "raw": "x"})
    main.run_pending_date_extractions()
    item = db.get_item(item_id)
    assert item["expiry_date"] == "2099-01-01"
    assert not item["date_extraction_pending"]


@pytest.mark.parametrize("peer,expected", [
    ("172.30.32.2", 200), ("127.0.0.1", 200), ("::1", 200), ("testclient", 200),
    ("192.0.2.4", 403),
])
def test_ingress_peer_allowlist_uses_socket_peer(peer, expected):
    class FakePeer:
        async def __call__(self, scope, receive, send):
            scope["client"] = (peer, 12345)
            await main.app(scope, receive, send)
    peer_client = TestClient(FakePeer())
    response = peer_client.get("/api/settings", headers={"X-Forwarded-For": "172.30.32.2"})
    assert response.status_code == expected


def test_check_now_returns_each_result_and_stores_summary(monkeypatch):
    results = {
        "sent": "Summary sent",
        "nothing_due": "Nothing is due right now",
        "failed": "Couldn't send the summary",
        "not_configured": "Notifications aren't set up",
    }
    for result, summary in results.items():
        monkeypatch.setattr(main.ha_sync, "notify_summary", lambda items, value=result: value)
        monkeypatch.setattr(main.ha_sync, "request_sync", lambda: None)
        response = client.post("/api/check-now")
        assert response.status_code == 200
        assert response.json() == {"result": result}
        assert db.get_state("last_check_summary") == summary


def test_product_image_display_priority():
    code = "5000000000010"
    db.cache_barcode(code, {"name": "Tin", "brand": "", "quantity": "", "category": "", "image_url": "https://off.example/image.jpg"})
    db.save_product_image(code, main.photos.normalise_product_photo(_jpeg_bytes(), max_dimension=400))
    local_url = main._display_image_url(code, "https://off.example/image.jpg")
    assert "/photo?v=" in local_url
    assert client.get(f"/api/barcodes/{code}/photo").status_code == 200
    db.save_product_photo(code, main.photos.normalise_product_photo(_jpeg_bytes()))
    import os
    os.utime(db.product_image_path(code), (100, 100))
    os.utime(db.product_photo_path(code), (200, 200))
    assert main._display_image_url(code, "https://off.example/image.jpg").endswith("v=200")


def test_list_reads_settings_and_barcode_cache_once(monkeypatch):
    db.cache_barcode("5000000000011", {"name": "Tin", "brand": "", "quantity": "", "category": "", "image_url": ""})
    db.add_item(barcode="5000000000011", name="First")
    db.add_item(barcode="5000000000011", name="Second")
    reads = {"settings": 0, "cache": 0, "single_cache": 0}
    original_get_int = main.settings.get_int
    original_get_many = db.get_cached_barcodes
    def counted_get_int(key):
        reads["settings"] += 1
        return original_get_int(key)
    def counted_get_many(codes):
        reads["cache"] += 1
        return original_get_many(codes)
    def forbidden_single(code):
        reads["single_cache"] += 1
        raise AssertionError("list opened a per-item cache connection")
    monkeypatch.setattr(main.settings, "get_int", counted_get_int)
    monkeypatch.setattr(db, "get_cached_barcodes", counted_get_many)
    monkeypatch.setattr(db, "get_cached_barcode", forbidden_single)
    response = client.get("/api/items")
    assert response.status_code == 200
    assert len(response.json()["items"]) == 2
    assert reads == {"settings": 1, "cache": 1, "single_cache": 0}



def test_lookup_unavailable_is_reported_separately(monkeypatch):
    def unavailable(code):
        raise main.lookup.LookupUnavailable("offline")
    monkeypatch.setattr(main.lookup, "lookup_barcode", unavailable)
    response = client.get("/api/lookup/5000000000020")
    assert response.json() == {"found": False, "unavailable": True, "display_image_url": ""}


def test_photo_retention_boundary_and_malformed_consumed_at(monkeypatch):
    from datetime import timedelta
    today = db.today()
    monkeypatch.setattr(db, "today", lambda: today)
    old_id = db.add_item(barcode=None, name="Old")
    boundary_id = db.add_item(barcode=None, name="Boundary")
    malformed_id = db.add_item(barcode=None, name="Malformed")
    active_id = db.add_item(barcode=None, name="Active")
    for item_id in [old_id, boundary_id, malformed_id, active_id]:
        db.save_date_photo(item_id, b"photo")
    with db._connect() as conn:
        conn.execute("UPDATE items SET has_date_photo=1 WHERE id=?", (active_id,))
        conn.execute("UPDATE items SET status=?,consumed_at=?,has_date_photo=1 WHERE id=?", (db.STATUS_CONSUMED, (today - timedelta(days=31)).isoformat(), old_id))
        conn.execute("UPDATE items SET status=?,consumed_at=?,has_date_photo=1 WHERE id=?", (db.STATUS_CONSUMED, (today - timedelta(days=30)).isoformat(), boundary_id))
        conn.execute("UPDATE items SET status=?,consumed_at=?,has_date_photo=1 WHERE id=?", (db.STATUS_CONSUMED, "malformed", malformed_id))
    assert db.expire_old_date_photos() == 1
    assert not db.date_photo_path(old_id).exists()
    assert db.get_item(old_id)["has_date_photo"] == 0
    for kept_id in [boundary_id, malformed_id, active_id]:
        assert db.date_photo_path(kept_id).exists()
        assert db.get_item(kept_id)["has_date_photo"] == 1
    assert db.get_item(malformed_id)["status"] == db.STATUS_CONSUMED
    assert db.get_item(active_id)["status"] == db.STATUS_ACTIVE


def test_local_product_image_route_has_changeable_cache_header():
    code = "5000000000021"
    db.save_product_image(code, main.photos.normalise_product_photo(_jpeg_bytes(), max_dimension=400))
    response = client.get(f"/api/barcodes/{code}/photo")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"



def test_product_image_download_host_allowlist_size_cap_and_no_raise(monkeypatch):
    calls = []
    class Response:
        def __init__(self, payload):
            self.payload = payload
        def read(self, limit):
            calls.append(limit)
            return self.payload[:limit]
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False
    monkeypatch.setattr(main.photos.urllib.request, "urlopen", lambda req, timeout: Response(_jpeg_bytes()))
    code = "5000000000022"
    assert main.photos.download_product_image(f"https://images.openfoodfacts.org/images/{code}.jpg", code)
    assert calls == [5 * 1024 * 1024]
    assert max(Image.open(db.product_image_path(code)).size) <= 400
    calls.clear()
    assert not main.photos.download_product_image(f"https://evilimages.openfoodfacts.org/{code}.jpg", code)
    assert not main.photos.download_product_image(f"http://images.openfoodfacts.org/{code}.jpg", code)
    assert calls == []

    db.product_image_path(code).unlink()
    monkeypatch.setattr(main.photos.urllib.request, "urlopen", lambda req, timeout: Response(b"x" * (5 * 1024 * 1024 + 1)))
    assert not main.photos.download_product_image(f"https://images.openfoodfacts.org/{code}.jpg", code)
    monkeypatch.setattr(main.photos.urllib.request, "urlopen", lambda req, timeout: (_ for _ in ()).throw(TimeoutError()))
    assert main.photos.download_product_image(f"https://images.openfoodfacts.org/{code}.jpg", code) is False
