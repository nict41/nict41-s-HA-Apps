import pytest
from fastapi.testclient import TestClient

import db
import main

# main.app's startup event (which starts the background scheduler) is only
# triggered if TestClient is used as a context manager; using it bare here
# keeps tests synchronous and avoids a real scheduler thread running during
# the suite.
client = TestClient(main.app)


@pytest.fixture(autouse=True)
def _fresh_db():
    db.DB_PATH.unlink(missing_ok=True)
    db.init_db()
    yield


def test_dashboard_loads_when_empty():
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Parcel Tracker" in resp.text


def test_add_parcel_creates_active_parcel_and_redirects():
    resp = client.post(
        "/add",
        data={"tracking_number": "1Z999AA10123456784", "carrier_name": "UPS", "description": "Cables"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    parcels = db.list_parcels()
    assert len(parcels) == 1
    assert parcels[0]["tracking_number"] == "1Z999AA10123456784"
    assert parcels[0]["status"] == db.STATUS_ACTIVE


def test_add_parcel_blank_tracking_number_is_ignored():
    client.post("/add", data={"tracking_number": "  "})
    assert db.list_parcels() == []


def test_confirm_moves_pending_to_active():
    parcel_id = db.upsert_parcel("ABC123", "Unknown", "test", 0.5, None, db.STATUS_PENDING)
    resp = client.post("/confirm", data={"parcel_id": parcel_id}, follow_redirects=False)
    assert resp.status_code == 303
    assert db.get_parcel(parcel_id)["status"] == db.STATUS_ACTIVE


def test_dismiss_marks_dismissed():
    parcel_id = db.upsert_parcel("ABC123", "Unknown", "test", 0.5, None, db.STATUS_PENDING)
    client.post("/dismiss", data={"parcel_id": parcel_id})
    assert db.get_parcel(parcel_id)["status"] == db.STATUS_DISMISSED


def test_archive_marks_archived():
    parcel_id = db.upsert_parcel("ABC123", "UPS", "test", 0.9, None, db.STATUS_ACTIVE)
    client.post("/archive", data={"parcel_id": parcel_id})
    assert db.get_parcel(parcel_id)["status"] == db.STATUS_ARCHIVED


def test_delete_removes_parcel():
    parcel_id = db.upsert_parcel("ABC123", "UPS", "test", 0.9, None, db.STATUS_ACTIVE)
    client.post("/delete", data={"parcel_id": parcel_id})
    assert db.get_parcel(parcel_id) is None


def test_sync_without_imap_configured_does_not_crash():
    resp = client.post("/sync", follow_redirects=False)
    assert resp.status_code == 303
    assert db.get_state("last_sync_at") is not None


def test_api_parcels_returns_json():
    db.upsert_parcel("ABC123", "UPS", "test", 0.9, None, db.STATUS_ACTIVE)
    resp = client.get("/api/parcels")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["parcels"]) == 1
    assert body["parcels"][0]["tracking_number"] == "ABC123"


def test_dashboard_lists_pending_and_active_parcels():
    db.upsert_parcel("PEND123", "Unknown", "maybe a parcel", 0.5, None, db.STATUS_PENDING)
    db.upsert_parcel("ACT123", "UPS", "definitely a parcel", 0.9, None, db.STATUS_ACTIVE)
    html = client.get("/").text
    assert "PEND123" in html
    assert "ACT123" in html


def test_dashboard_shows_email_preview_for_pending_parcel_with_source_email():
    db.upsert_parcel(
        "PEND123",
        "Unknown",
        "maybe a parcel",
        0.5,
        "msg-1",
        db.STATUS_PENDING,
        email_sender="newsletter@example.com",
        email_subject="Your order update",
        email_body="Hello, your tracking number is PEND123 and it ships soon.",
    )
    html = client.get("/").text
    assert "View full email" in html
    assert "newsletter@example.com" in html
    assert "Your order update" in html


def test_dashboard_omits_email_preview_when_no_source_email():
    db.upsert_parcel("PEND123", "Unknown", "manual add", 0.5, None, db.STATUS_PENDING)
    html = client.get("/").text
    assert "View full email" not in html
