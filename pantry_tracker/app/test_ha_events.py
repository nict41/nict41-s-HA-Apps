import db
import ha_events
import json


def test_handle_action_accepts_full_actions_and_ignores_invalid_or_inactive(monkeypatch):
    synced = []
    monkeypatch.setattr(ha_events.ha_sync, "request_sync", lambda: synced.append(True))
    item_id = db.add_item(barcode=None, name="Milk", count=2)
    ha_events.handle_action(f"PANTRY_EATEN_{item_id}")
    assert db.get_item(item_id)["count"] == 1
    assert len(synced) == 1
    ha_events.handle_action(f"PANTRY_EATEN_{item_id}extra")
    assert db.get_item(item_id)["count"] == 1
    db.update_item(item_id, status=db.STATUS_CONSUMED)
    ha_events.handle_action(f"PANTRY_BINNED_{item_id}")
    assert len(synced) == 1


def test_handle_action_snoozes_active_items(monkeypatch):
    monkeypatch.setattr(ha_events.ha_sync, "request_sync", lambda: None)
    first = db.add_item(barcode=None, name="Milk")
    second = db.add_item(barcode=None, name="Soup")
    ha_events.handle_action(f"PANTRY_SNOOZE_{first},{second}")
    assert db.get_item(first)["snoozed_until"] == (db.today() + __import__("datetime").timedelta(days=2)).isoformat()


def test_handle_action_rejects_long_ids_and_non_string_actions(monkeypatch):
    calls = []
    monkeypatch.setattr(ha_events.ha_sync, "request_sync", lambda: calls.append(True))
    item_id = db.add_item(barcode=None, name="Milk")
    ha_events.handle_action(f"PANTRY_EATEN_{item_id:010d}")
    ha_events.handle_action(None)
    assert db.get_item(item_id)["status"] == db.STATUS_ACTIVE
    assert calls == []


def test_repeated_finish_action_does_not_finish_inactive_item_twice(monkeypatch):
    synced = []
    monkeypatch.setattr(ha_events.ha_sync, "request_sync", lambda: synced.append(True))
    item_id = db.add_item(barcode=None, name="Milk", count=1)
    action = f"PANTRY_EATEN_{item_id}"
    ha_events.handle_action(action)
    with db._connect() as conn:
        first_events = conn.execute("SELECT COUNT(*) FROM item_events WHERE item_id=?", (item_id,)).fetchone()[0]
    ha_events.handle_action(action)
    assert db.get_item(item_id)["status"] == db.STATUS_CONSUMED
    with db._connect() as conn:
        event_count = conn.execute("SELECT COUNT(*) FROM item_events WHERE item_id=?", (item_id,)).fetchone()[0]
    assert event_count == first_events == 1
    assert len(synced) == 1


def test_handle_action_freezes_valid_repeated_and_ignores_consumed_or_malformed(monkeypatch):
    calls = []
    monkeypatch.setattr(ha_events.ha_sync, "request_sync", lambda: calls.append("sync"))
    item_id = db.add_item(barcode=None, name="Milk", location="fridge")
    ha_events.handle_action(f"PANTRY_FREEZE_{item_id}")
    assert db.get_item(item_id)["location"] == "freezer"
    assert len(calls) == 1
    ha_events.handle_action(f"PANTRY_FREEZE_{item_id}")
    assert len(calls) == 1
    consumed = db.add_item(barcode=None, name="Soup")
    db.finish_item(consumed, "eaten")
    ha_events.handle_action(f"PANTRY_FREEZE_{consumed}")
    ha_events.handle_action(f"PANTRY_FREEZE_{item_id}extra")
    ha_events.handle_action("PANTRY_FREEZE_1234567890")
    assert db.get_item(consumed)["location"] != "freezer"
    assert len(calls) == 1


def test_handle_action_does_not_change_remembered_location(monkeypatch):
    monkeypatch.setattr(ha_events.ha_sync, "request_sync", lambda: None)
    item_id = db.add_item(barcode="ha-memory", name="Milk", location="fridge")
    db.cache_barcode("ha-memory", {"name": "Milk", "image_url": ""})
    db.update_cached_barcode("ha-memory", location="cupboard")
    ha_events.handle_action(f"PANTRY_FREEZE_{item_id}")
    assert db.get_cached_barcode("ha-memory")["location"] == "cupboard"


def test_action_handler_exception_is_logged_without_dropping_socket(monkeypatch, capsys):
    import websockets.sync.client

    class FakeSocket:
        def __init__(self):
            self.messages = [
                {"type": "auth_required"},
                {"type": "auth_ok"},
                {"success": True},
                {"event": {"data": {"action": "first"}}},
                {"event": {"data": {"action": "second"}}},
            ]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def recv(self, timeout=None):
            if timeout is not None and len(self.messages) == 1:
                stop.set()
            return json.dumps(self.messages.pop(0))

        def send(self, _message):
            return None

    socket = FakeSocket()
    stop = __import__("threading").Event()
    calls = []
    monkeypatch.setattr(ha_events.ha_sync, "SUPERVISOR_TOKEN", "test-token")
    monkeypatch.setattr(websockets.sync.client, "connect", lambda *_args, **_kwargs: socket)

    def handle(action):
        calls.append(action)
        if action == "first":
            raise RuntimeError("bad action")

    monkeypatch.setattr(ha_events, "handle_action", handle)
    ha_events.run(stop)
    assert calls == ["first", "second"]
    output = capsys.readouterr().out
    assert "action failed: RuntimeError" in output
    assert "test-token" not in output
