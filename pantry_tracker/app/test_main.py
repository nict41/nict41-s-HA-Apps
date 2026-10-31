from io import BytesIO

import db
import main
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
        "display_image_url": "",
    }
    assert calls == ["9999"]


def test_lookup_product_not_found(monkeypatch):
    monkeypatch.setattr(main.lookup, "lookup_barcode", lambda code: None)
    res = client.get("/api/lookup/123")
    assert res.json() == {"found": False, "display_image_url": ""}


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

    res = client.post(f"/api/items/{item['id']}/consume")
    assert res.status_code == 200
    assert db.get_item(item["id"])["status"] == db.STATUS_CONSUMED

    res = client.post(f"/api/items/{item['id']}/delete")
    assert res.status_code == 200
    assert db.get_item(item["id"]) is None


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
            "check_interval_hours": 8,
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["notify_days_before"] == 5
    assert body["notify_repeat_hours"] == 48
    assert body["check_interval_hours"] == 8

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
            "check_interval_hours": 8,
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
    assert res.json()["ok"] is True


def test_export_returns_json_attachment():
    client.post("/api/items", data={"name": "Export Me"})
    res = client.get("/export")
    assert res.status_code == 200
    assert "attachment" in res.headers["content-disposition"]
    assert any(i["name"] == "Export Me" for i in res.json()["items"])


def test_parse_date_endpoint(monkeypatch):
    monkeypatch.setattr(main.vision, "parse_transcript", lambda text: "2027-03-01" if text == "01 03 27" else None)
    assert client.post("/api/parse-date", data={"text": "01 03 27"}).json() == {"date": "2027-03-01"}
    assert client.post("/api/parse-date", data={"text": "nothing"}).json() == {"date": None}


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
