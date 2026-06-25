import json

import pytest

import carriers
import db
import ha_sync


@pytest.fixture(autouse=True)
def _fresh_db():
    db.DB_PATH.unlink(missing_ok=True)
    db.init_db()
    yield


@pytest.fixture
def _configured(monkeypatch):
    monkeypatch.setattr(ha_sync, "SUPERVISOR_TOKEN", "test-token")


@pytest.fixture
def _calls(monkeypatch, _configured):
    calls: list[tuple[str, str, dict | None]] = []
    monkeypatch.setattr(ha_sync, "_request", lambda method, entity_id, body=None: calls.append((method, entity_id, body)))
    return calls


def _make_parcel(tracking_number, status, **overrides):
    parcel_id = db.upsert_parcel(tracking_number, overrides.get("carrier_name", "UPS"), overrides.get("description", "a widget"), 0.9, None, status)
    return db.get_parcel(parcel_id)


def test_not_configured_makes_no_requests(monkeypatch):
    monkeypatch.setattr(ha_sync, "SUPERVISOR_TOKEN", "")
    called = []
    monkeypatch.setattr(ha_sync, "_request", lambda *a, **k: called.append(1))
    ha_sync.sync([_make_parcel("A", db.STATUS_ACTIVE)])
    assert called == []


def test_entity_id_for_is_a_lowercased_slug_of_the_tracking_number():
    parcel = _make_parcel("1Z-999.AA10123456784", db.STATUS_ACTIVE)
    assert ha_sync.entity_id_for(parcel) == "sensor.parcel_tracker_1z_999_aa10123456784"


def test_sync_posts_summary_and_per_parcel_state(_calls):
    parcel = _make_parcel("ABC123", db.STATUS_ACTIVE, description="Cables")
    ha_sync.sync([parcel])

    posts = {entity_id: body for method, entity_id, body in _calls if method == "POST"}
    assert "sensor.parcel_tracker_summary" in posts
    assert posts["sensor.parcel_tracker_summary"]["state"] == "1"
    assert posts["sensor.parcel_tracker_summary"]["attributes"]["in_transit"] == 1

    parcel_entity_id = ha_sync.entity_id_for(parcel)
    assert posts[parcel_entity_id]["state"] == db.STATUS_ACTIVE
    assert posts[parcel_entity_id]["attributes"]["tracking_number"] == "ABC123"
    assert posts[parcel_entity_id]["attributes"]["friendly_name"] == "Cables"

    summary_parcel = posts["sensor.parcel_tracker_summary"]["attributes"]["parcels"][0]
    assert summary_parcel["tracking_number"] == "ABC123"
    assert summary_parcel["tracking_url"] == carriers.get_tracking_url("ABC123")


def test_archived_and_dismissed_parcels_are_excluded_from_sync(_calls):
    active = _make_parcel("ACT1", db.STATUS_ACTIVE)
    archived = _make_parcel("ARC1", db.STATUS_ARCHIVED)
    dismissed = _make_parcel("DIS1", db.STATUS_DISMISSED)

    ha_sync.sync([active, archived, dismissed])

    posted_ids = {entity_id for method, entity_id, _ in _calls if method == "POST"}
    assert ha_sync.entity_id_for(active) in posted_ids
    assert ha_sync.entity_id_for(archived) not in posted_ids
    assert ha_sync.entity_id_for(dismissed) not in posted_ids


def test_sync_deletes_entity_once_parcel_is_archived(_calls):
    parcel = _make_parcel("XYZ1", db.STATUS_ACTIVE)
    ha_sync.sync([parcel])
    _calls.clear()

    db.archive_parcel(parcel["id"])
    archived_parcel = db.get_parcel(parcel["id"])
    ha_sync.sync([archived_parcel])

    deletes = [(method, entity_id) for method, entity_id, _ in _calls if method == "DELETE"]
    assert ("DELETE", ha_sync.entity_id_for(parcel)) in deletes


def test_synced_entity_ids_are_persisted_across_calls(_calls):
    parcel = _make_parcel("PERSIST1", db.STATUS_ACTIVE)
    ha_sync.sync([parcel])
    stored = json.loads(db.get_state(ha_sync._SYNCED_IDS_STATE_KEY))
    assert stored == [ha_sync.entity_id_for(parcel)]
