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


def test_summary_includes_needs_date_count():
    state = ha_sync._summary_state([(_make_item(expiry_date=None), db.BUCKET_UNKNOWN)])
    assert state["attributes"]["needs_date"] == 1


def test_summary_contains_effective_and_printed_dates_and_frozen_count():
    item = _make_item(expiry_date="2030-02-01", location="freezer", count=2)
    state = ha_sync._summary_state([(item, db.BUCKET_FROZEN)])
    assert state["attributes"]["frozen"] == 1
    entry = state["attributes"]["items"][0]
    assert entry["expiry_date"] is None
    assert entry["printed_date"] == "2030-02-01"
    assert entry["count"] == 2


def test_summary_notification_marks_only_after_success(monkeypatch):
    monkeypatch.setenv("NOTIFY_SERVICE", "notify.phone")
    monkeypatch.setattr(ha_sync, "_request", lambda *args, **kwargs: False)
    item_id = db.add_item(barcode=None, name="Milk", expiry_date="2020-01-01")
    assert ha_sync.notify_summary([db.get_item(item_id)]) == 0
    assert db.get_item(item_id)["last_notified_at"] is None


def test_summary_notification_text_and_actions(monkeypatch):
    monkeypatch.setenv("NOTIFY_SERVICE", "notify.phone")
    calls = []
    monkeypatch.setattr(ha_sync, "_request", lambda method, path, body=None: calls.append(body) or True)
    item_id = db.add_item(barcode=None, name="Milk", count=3, expiry_date="2020-01-01")
    assert ha_sync.notify_summary([db.get_item(item_id)]) == 1
    payload = calls[0]
    assert payload["title"] == "1 item to use"
    assert payload["message"] == "Expired: Milk ×3."
    assert "day(s)" not in payload["message"]
    assert "2020-01-01" not in payload["message"]
    assert [action["action"] for action in payload["data"]["actions"]] == [
        f"PANTRY_EATEN_{item_id}", f"PANTRY_BINNED_{item_id}", f"PANTRY_SNOOZE_{item_id}"
    ]


def test_multiple_items_make_one_summary_with_snooze_only(monkeypatch):
    monkeypatch.setenv("NOTIFY_SERVICE", "notify.phone")
    calls = []
    monkeypatch.setattr(ha_sync, "_request", lambda method, path, body=None: calls.append(body) or True)
    first = db.add_item(barcode=None, name="Milk", expiry_date=(db.today() + timedelta(days=1)).isoformat())
    second = db.add_item(barcode=None, name="Soup", expiry_date=(db.today() + timedelta(days=2)).isoformat())
    assert ha_sync.notify_summary([db.get_item(first), db.get_item(second)]) == 1
    payload = calls[0]
    assert "Tomorrow: Milk." in payload["message"]
    assert "In 2 days: Soup." in payload["message"]
    assert payload["data"]["actions"] == [{"action": f"PANTRY_SNOOZE_{first},{second}", "title": "Snooze"}]


def test_snooze_and_bucket_change_due(monkeypatch):
    item_id = db.add_item(barcode=None, name="Milk", expiry_date="2020-01-01")
    db.update_item(item_id, snoozed_until=(db.today() + timedelta(days=2)).isoformat())
    db.update_item(item_id, last_notified_bucket=db.BUCKET_EXPIRED,
                   last_notified_at=datetime.now(timezone.utc).isoformat())
    item = db.get_item(item_id)
    assert ha_sync._due_for_notification(item, db.BUCKET_EXPIRED) is False
    db.update_item(item_id, last_notified_bucket=db.BUCKET_EXPIRING_SOON, last_notified_at=datetime.now(timezone.utc).isoformat())
    assert ha_sync._due_for_notification(db.get_item(item_id), db.BUCKET_EXPIRED) is True


def test_repeat_window_allows_one_hour_of_slack(monkeypatch):
    item_id = db.add_item(barcode=None, name="Milk", expiry_date="2020-01-01")
    last_at = (datetime.now(timezone.utc) - timedelta(hours=23, minutes=5)).isoformat()
    db.update_item(item_id, last_notified_bucket=db.BUCKET_EXPIRED, last_notified_at=last_at)
    assert ha_sync._due_for_notification(db.get_item(item_id), db.BUCKET_EXPIRED) is True


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


def test_notify_summary_requires_configured_service(monkeypatch):
    monkeypatch.setenv("NOTIFY_SERVICE", "")
    assert ha_sync.notify_summary([_make_item(expiry_date="2020-01-01")]) == 0


def test_notify_summary_skips_non_due_item(monkeypatch):
    monkeypatch.setenv("NOTIFY_SERVICE", "notify.mobile_app_phone")
    monkeypatch.setattr(ha_sync, "_request", lambda *a, **k: True)
    assert ha_sync.notify_summary([_make_item(expiry_date="2099-01-01")]) == 0


def test_notify_summary_sends_and_marks_notified(monkeypatch):
    monkeypatch.setenv("NOTIFY_SERVICE", "notify.mobile_app_phone")
    calls = []
    monkeypatch.setattr(ha_sync, "_request", lambda method, path, body=None: calls.append((method, path, body)) or True)

    item_id = db.add_item(barcode=None, name="Yoghurt", expiry_date="2020-01-01")
    item = db.get_item(item_id)

    sent = ha_sync.notify_summary([item])
    assert sent == 1
    assert calls[0] == ("POST", "/services/notify/mobile_app_phone", calls[0][2])
    assert "Yoghurt" in calls[0][2]["message"]

    refreshed = db.get_item(item_id)
    assert refreshed["last_notified_bucket"] == db.BUCKET_EXPIRED
    assert refreshed["last_notified_at"] is not None


def test_notify_summary_sets_click_action_to_own_ingress_panel(monkeypatch):
    monkeypatch.setenv("NOTIFY_SERVICE", "notify.mobile_app_phone")
    # The real hostname format, from `ha apps info` - hyphens, not the slug.
    monkeypatch.setattr(ha_sync.socket, "gethostname", lambda: "beb500c8-pantry-tracker")
    calls = []
    monkeypatch.setattr(ha_sync, "_request", lambda method, path, body=None: calls.append((method, path, body)) or True)

    item_id = db.add_item(barcode=None, name="Yoghurt", expiry_date="2020-01-01")
    ha_sync.notify_summary([db.get_item(item_id)])

    assert calls[0][2]["data"]["clickAction"] == "/beb500c8_pantry_tracker/pantry"
    assert calls[0][2]["data"]["url"] == "/beb500c8_pantry_tracker/pantry"


def test_notify_summary_respects_repeat_window(monkeypatch):
    monkeypatch.setenv("NOTIFY_SERVICE", "notify.mobile_app_phone")
    settings.set_many({"notify_repeat_hours": 24})
    calls = []
    monkeypatch.setattr(ha_sync, "_request", lambda method, path, body=None: calls.append(1) or True)

    item_id = db.add_item(barcode=None, name="Cheese", expiry_date="2020-01-01")
    db.mark_notified(item_id, db.BUCKET_EXPIRED)
    item = db.get_item(item_id)

    # Just notified moments ago, in the same bucket - no repeat yet.
    assert ha_sync.notify_summary([item]) == 0
    assert calls == []


def test_notify_summary_sends_again_after_repeat_window(monkeypatch):
    monkeypatch.setenv("NOTIFY_SERVICE", "notify.mobile_app_phone")
    settings.set_many({"notify_repeat_hours": 24})
    calls = []
    monkeypatch.setattr(ha_sync, "_request", lambda method, path, body=None: calls.append(1) or True)

    item_id = db.add_item(barcode=None, name="Cheese", expiry_date="2020-01-01")
    stale = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(timespec="seconds")
    db.update_item(item_id, last_notified_at=stale, last_notified_bucket=db.BUCKET_EXPIRED)
    item = db.get_item(item_id)

    assert ha_sync.notify_summary([item]) == 1
    assert calls == [1]


def test_notify_summary_bucket_change_bypasses_repeat_window(monkeypatch):
    monkeypatch.setenv("NOTIFY_SERVICE", "notify.mobile_app_phone")
    settings.set_many({"notify_repeat_hours": 24})
    calls = []
    monkeypatch.setattr(ha_sync, "_request", lambda method, path, body=None: calls.append(1) or True)

    item_id = db.add_item(barcode=None, name="Cheese", expiry_date="2020-01-01")
    db.mark_notified(item_id, db.BUCKET_EXPIRING_SOON)
    item = db.get_item(item_id)

    # Degraded from expiring_soon to expired just now - should alert immediately.
    assert ha_sync.notify_summary([item]) == 1
    assert calls == [1]
