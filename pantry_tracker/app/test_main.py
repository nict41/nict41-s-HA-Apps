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


def test_lookup_product_not_found(monkeypatch):
    monkeypatch.setattr(main.lookup, "lookup_barcode", lambda code: None)
    res = client.get("/api/lookup/123")
    assert res.json() == {"found": False}


def test_extract_date(monkeypatch):
    monkeypatch.setattr(main.vision, "extract_use_by_date", lambda image_bytes: {"date": "2026-12-01", "raw": "2026-12-01"})
    res = client.post("/api/extract-date", files={"photo": ("p.jpg", b"fake-bytes", "image/jpeg")})
    assert res.json() == {"date": "2026-12-01", "raw": "2026-12-01"}


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
