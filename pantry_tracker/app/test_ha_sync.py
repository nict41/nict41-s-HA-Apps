from datetime import datetime, timedelta, timezone

import db
import ha_sync
import settings


def _make_item(**overrides):
    defaults = {
        "id": 1,
        "name": "Milk",
        "brand": "Tesco",
        "quantity": "1L",
        "category": "Fridge",
        "expiry_date": None,
        "barcode": "123",
        "last_notified_at": None,
        "last_notified_bucket": None,
    }
    defaults.update(overrides)
    return defaults


def test_not_configured_without_supervisor_token(monkeypatch):
    monkeypatch.setattr(ha_sync, "SUPERVISOR_TOKEN", "")
    assert ha_sync.configured() is False
    # sync() must no-op cleanly, not raise, when there's no token.
    ha_sync.sync([_make_item()])


def test_entity_id_is_slugified_and_id_prefixed():
    item = _make_item(id=7, name="Baked Beans (Heinz)!")
    assert ha_sync.entity_id_for(item) == "sensor.pantry_tracker_7_baked_beans_heinz"


def test_sync_pushes_summary_and_item_states(monkeypatch):
    monkeypatch.setattr(ha_sync, "SUPERVISOR_TOKEN", "fake-token")
    calls = []
    monkeypatch.setattr(ha_sync, "_request", lambda method, path, body=None: calls.append((method, path, body)) or True)

    item = _make_item(expiry_date=None)
    ha_sync.sync([item])

    paths = [c[1] for c in calls if c[0] == "POST"]
    assert "/states/sensor.pantry_tracker_summary" in paths
    assert f"/states/{ha_sync.entity_id_for(item)}" in paths


def test_sync_deletes_stale_entities(monkeypatch):
    monkeypatch.setattr(ha_sync, "SUPERVISOR_TOKEN", "fake-token")
    calls = []
    monkeypatch.setattr(ha_sync, "_request", lambda method, path, body=None: calls.append((method, path, body)) or True)

    first_item = _make_item(id=1, name="Milk")
    ha_sync.sync([first_item])

    second_item = _make_item(id=2, name="Eggs")
    calls.clear()
    ha_sync.sync([second_item])

    deletes = [c[1] for c in calls if c[0] == "DELETE"]
    assert deletes == [f"/states/{ha_sync.entity_id_for(first_item)}"]


def test_notify_if_due_requires_configured_service(monkeypatch):
    monkeypatch.setenv("NOTIFY_SERVICE", "")
    assert ha_sync.notify_if_due(_make_item(), db.BUCKET_EXPIRED) is False


def test_notify_if_due_skips_ok_bucket(monkeypatch):
    monkeypatch.setenv("NOTIFY_SERVICE", "notify.mobile_app_phone")
    monkeypatch.setattr(ha_sync, "_request", lambda *a, **k: True)
    assert ha_sync.notify_if_due(_make_item(), db.BUCKET_OK) is False


def test_notify_if_due_sends_and_marks_notified(monkeypatch):
    monkeypatch.setenv("NOTIFY_SERVICE", "notify.mobile_app_phone")
    calls = []
    monkeypatch.setattr(ha_sync, "_request", lambda method, path, body=None: calls.append((method, path, body)) or True)

    item_id = db.add_item(barcode=None, name="Yoghurt", expiry_date="2020-01-01")
    item = db.get_item(item_id)

    sent = ha_sync.notify_if_due(item, db.BUCKET_EXPIRED)
    assert sent is True
    assert calls[0] == ("POST", "/services/notify/mobile_app_phone", calls[0][2])
    assert "Yoghurt" in calls[0][2]["message"]

    refreshed = db.get_item(item_id)
    assert refreshed["last_notified_bucket"] == db.BUCKET_EXPIRED
    assert refreshed["last_notified_at"] is not None


def test_notify_if_due_sets_click_action_to_own_ingress_panel(monkeypatch):
    monkeypatch.setenv("NOTIFY_SERVICE", "notify.mobile_app_phone")
    # The real hostname format, from `ha apps info` - hyphens, not the slug.
    monkeypatch.setattr(ha_sync.socket, "gethostname", lambda: "beb500c8-pantry-tracker")
    calls = []
    monkeypatch.setattr(ha_sync, "_request", lambda method, path, body=None: calls.append((method, path, body)) or True)

    item_id = db.add_item(barcode=None, name="Yoghurt", expiry_date="2020-01-01")
    ha_sync.notify_if_due(db.get_item(item_id), db.BUCKET_EXPIRED)

    assert calls[0][2]["data"]["clickAction"] == "/beb500c8_pantry_tracker/pantry"
    assert calls[0][2]["data"]["url"] == "/beb500c8_pantry_tracker/pantry"


def test_notify_if_due_respects_repeat_window(monkeypatch):
    monkeypatch.setenv("NOTIFY_SERVICE", "notify.mobile_app_phone")
    settings.set_many({"notify_repeat_hours": 24})
    calls = []
    monkeypatch.setattr(ha_sync, "_request", lambda method, path, body=None: calls.append(1) or True)

    item_id = db.add_item(barcode=None, name="Cheese", expiry_date="2020-01-01")
    db.mark_notified(item_id, db.BUCKET_EXPIRED)
    item = db.get_item(item_id)

    # Just notified moments ago, in the same bucket - no repeat yet.
    assert ha_sync.notify_if_due(item, db.BUCKET_EXPIRED) is False
    assert calls == []


def test_notify_if_due_sends_again_after_repeat_window(monkeypatch):
    monkeypatch.setenv("NOTIFY_SERVICE", "notify.mobile_app_phone")
    settings.set_many({"notify_repeat_hours": 24})
    calls = []
    monkeypatch.setattr(ha_sync, "_request", lambda method, path, body=None: calls.append(1) or True)

    item_id = db.add_item(barcode=None, name="Cheese", expiry_date="2020-01-01")
    stale = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(timespec="seconds")
    db.update_item(item_id, last_notified_at=stale, last_notified_bucket=db.BUCKET_EXPIRED)
    item = db.get_item(item_id)

    assert ha_sync.notify_if_due(item, db.BUCKET_EXPIRED) is True
    assert calls == [1]


def test_notify_if_due_bucket_change_bypasses_repeat_window(monkeypatch):
    monkeypatch.setenv("NOTIFY_SERVICE", "notify.mobile_app_phone")
    settings.set_many({"notify_repeat_hours": 24})
    calls = []
    monkeypatch.setattr(ha_sync, "_request", lambda method, path, body=None: calls.append(1) or True)

    item_id = db.add_item(barcode=None, name="Cheese", expiry_date="2020-01-01")
    db.mark_notified(item_id, db.BUCKET_EXPIRING_SOON)
    item = db.get_item(item_id)

    # Degraded from expiring_soon to expired just now - should alert immediately.
    assert ha_sync.notify_if_due(item, db.BUCKET_EXPIRED) is True
    assert calls == [1]
