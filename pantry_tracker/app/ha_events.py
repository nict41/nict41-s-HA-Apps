"""Home Assistant mobile notification actions."""

import re
import threading
import json
from datetime import timedelta

import db
import ha_sync

_ACTION = re.compile(r"PANTRY_(EATEN|BINNED)_(\d{1,9})|PANTRY_SNOOZE_(\d{1,9}(?:,\d{1,9})*)")


def handle_action(action: str) -> None:
    if not isinstance(action, str):
        return
    match = _ACTION.fullmatch(action)
    if not match:
        return
    if match.group(3):
        until = (db.today() + timedelta(days=2)).isoformat()
        for value in match.group(3).split(","):
            item = db.get_item(int(value))
            if item and item["status"] == db.STATUS_ACTIVE:
                db.update_item(item["id"], snoozed_until=until)
    else:
        item_id = int(match.group(2))
        item = db.get_item(item_id)
        if not item or item["status"] != db.STATUS_ACTIVE:
            return
        db.finish_item(item_id, match.group(1).lower())
    ha_sync.sync(db.list_items([db.STATUS_ACTIVE]))


def run(stop: threading.Event) -> None:
    from websockets.sync.client import connect

    delay = 1
    while not stop.is_set():
        try:
            with connect("ws://supervisor/core/websocket", open_timeout=10) as socket:
                socket.recv()
                socket.send(json.dumps({"type": "auth", "access_token": ha_sync.SUPERVISOR_TOKEN}))
                auth_result = json.loads(socket.recv())
                if auth_result.get("type") != "auth_ok":
                    raise RuntimeError("Home Assistant authentication failed")
                socket.send(json.dumps({"id": 1, "type": "subscribe_events", "event_type": "mobile_app_notification_action"}))
                subscription = json.loads(socket.recv())
                if not subscription.get("success"):
                    raise RuntimeError("Home Assistant action subscription failed")
                print("[pantry_tracker] Home Assistant action listener connected", flush=True)
                delay = 1
                while not stop.is_set():
                    try:
                        message = json.loads(socket.recv(timeout=1))
                    except TimeoutError:
                        continue
                    event = message.get("event", {}).get("data", {})
                    try:
                        handle_action(event.get("action"))
                    except Exception as exc:
                        print(f"[pantry_tracker] Home Assistant action failed: {type(exc).__name__}", flush=True)
        except Exception as exc:
            print(f"[pantry_tracker] Home Assistant action listener failed: {type(exc).__name__}", flush=True)
            stop.wait(delay)
            delay = min(delay * 2, 60)
