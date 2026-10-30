import db
import main
from fastapi.testclient import TestClient

client = TestClient(main.app)


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
        lambda code: {"name": "Milk", "brand": "Tesco", "quantity": "1L", "category": "Dairy", "image_url": ""},
    )
    res = client.get("/api/lookup/123")
    assert res.json()["found"] is True
    assert res.json()["name"] == "Milk"
    assert res.json()["cached"] is False


def test_lookup_product_caches_and_skips_network_on_repeat_scan(monkeypatch):
    calls = []
    monkeypatch.setattr(
        main.lookup,
        "lookup_barcode",
        lambda code: calls.append(code) or {"name": "Beans", "brand": "Heinz", "quantity": "415g", "category": "Tinned", "image_url": ""},
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
    }
    assert calls == ["9999"]


def test_lookup_product_not_found(monkeypatch):
    monkeypatch.setattr(main.lookup, "lookup_barcode", lambda code: None)
    res = client.get("/api/lookup/123")
    assert res.json() == {"found": False}


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

    res = client.post(f"/api/items/{item['id']}/consume")
    assert res.status_code == 200
    assert db.get_item(item["id"])["status"] == db.STATUS_CONSUMED

    res = client.post(f"/api/items/{item['id']}/delete")
    assert res.status_code == 200
    assert db.get_item(item["id"]) is None


def test_edit_missing_item_returns_404():
    res = client.post("/api/items/999999", data={"name": "Ghost"})
    assert res.status_code == 404


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
            "check_interval_hours": 8,
            "notify_service": "notify.mobile_app_phone",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["notify_days_before"] == 5
    assert body["notify_repeat_hours"] == 48
    assert body["check_interval_hours"] == 8
    assert body["notify_service"] == "notify.mobile_app_phone"

    res = client.get("/api/settings")
    assert res.json()["notify_days_before"] == 5


def test_check_now_runs_without_ha_configured():
    res = client.post("/api/check-now")
    assert res.status_code == 200
    assert res.json()["ok"] is True


def test_export_returns_json_attachment():
    client.post("/api/items", data={"name": "Export Me"})
    res = client.get("/export")
    assert res.status_code == 200
    assert "attachment" in res.headers["content-disposition"]
    assert any(i["name"] == "Export Me" for i in res.json()["items"])
