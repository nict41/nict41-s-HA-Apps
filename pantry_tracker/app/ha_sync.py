"""Exposes pantry items to Home Assistant as sensor entities, and sends
one summary notification through a configured `notify.*` service when
items become due. Both use the add-on's own Supervisor-granted access to
the Home Assistant Core API (`homeassistant_api: true` in config.yaml), so
no MQTT setup or extra credentials are needed - the same pattern Parcel
Tracker uses elsewhere in this repo.

One `sensor.pantry_tracker_summary` entity carries the full item list as an
attribute. One `sensor.pantry_tracker_<id>_<name>` entity per active item
exists so automations can trigger on a specific item's state, the same
reasoning Parcel Tracker applies to per-parcel entities.

States set this way aren't registered with any integration, so they don't
survive a Home Assistant restart on their own - but a separate five-minute
job checks the summary sensor after startup and republishes the entities if
Home Assistant lost them.
"""

import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import db
import settings

SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "").strip()

_BASE_URL = "http://supervisor/core/api"
_ENTITY_PREFIX = "sensor.pantry_tracker_"
_SUMMARY_ENTITY_ID = "sensor.pantry_tracker_summary"
_SYNCED_IDS_STATE_KEY = "ha_synced_entity_ids"

_BUCKET_ICONS = {
    db.BUCKET_OK: "mdi:food-apple-outline",
    db.BUCKET_EXPIRING_SOON: "mdi:clock-alert-outline",
    db.BUCKET_EXPIRED: "mdi:alert-circle",
    db.BUCKET_UNKNOWN: "mdi:help-circle-outline",
    db.BUCKET_FROZEN: "mdi:snowflake",
}

# Buckets worth alerting on. OK, unknown, and frozen items have no current
# use-by warning to send.
_NOTIFIABLE_BUCKETS = (db.BUCKET_EXPIRING_SOON, db.BUCKET_EXPIRED)
_SYNC_REQUESTED = threading.Event()
_SYNC_STOP = threading.Event()
_SYNC_THREAD = None
_SYNC_LOCK = threading.Lock()


def request_sync() -> None:
    """Requests a background refresh; repeated requests are coalesced."""
    if configured() and _SYNC_THREAD is not None and _SYNC_THREAD.is_alive():
        _SYNC_REQUESTED.set()


def _sync_worker() -> None:
    while not _SYNC_STOP.is_set():
        if not _SYNC_REQUESTED.wait(0.5):
            continue
        # Let a burst of mutations settle, then read the latest item list.
        time.sleep(0.1)
        _SYNC_REQUESTED.clear()
        try:
            sync(db.list_items([db.STATUS_ACTIVE]))
        except Exception as exc:
            print(f"[pantry_tracker] Background sync failed: {type(exc).__name__}", flush=True)


def start_sync_worker() -> None:
    global _SYNC_THREAD
    if not configured():
        return
    with _SYNC_LOCK:
        if _SYNC_THREAD is not None and _SYNC_THREAD.is_alive():
            return
        _SYNC_STOP.clear()
        _SYNC_REQUESTED.clear()
        _SYNC_THREAD = threading.Thread(target=_sync_worker, name="pantry-ha-sync", daemon=True)
        _SYNC_THREAD.start()


def stop_sync_worker() -> None:
    global _SYNC_THREAD
    with _SYNC_LOCK:
        thread = _SYNC_THREAD
        _SYNC_STOP.set()
    if thread is not None:
        thread.join()
    with _SYNC_LOCK:
        if _SYNC_THREAD is thread:
            _SYNC_THREAD = None


def configured() -> bool:
    return bool(SUPERVISOR_TOKEN)


def summary_state_exists() -> bool | None:
    """Checks whether the summary entity survived a Home Assistant restart."""
    if not configured():
        return None
    req = urllib.request.Request(
        f"{_BASE_URL}/states/{_SUMMARY_ENTITY_ID}",
        headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            return True
    except urllib.error.HTTPError as exc:
        return False if exc.code == 404 else None
    except (urllib.error.URLError, TimeoutError):
        return None


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_") or "item"


def entity_id_for(item: dict) -> str:
    return f"{_ENTITY_PREFIX}{item['id']}_{_slugify(item['name'])}"


def _request(method: str, path: str, body: dict | None = None) -> bool:
    """Returns whether the request was sent successfully. Never raises -
    a Home Assistant restart or network hiccup should never crash a check
    cycle, just skip that one sync/notification for this round."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        f"{_BASE_URL}{path}",
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        return True
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"[pantry_tracker] Home Assistant API request to '{path}' failed: {exc}")
        return False


def _item_state(item: dict, bucket: str) -> dict:
    return {
        "state": bucket,
        "attributes": {
            "friendly_name": item["name"],
            "icon": _BUCKET_ICONS.get(bucket, "mdi:food-apple-outline"),
            "brand": item["brand"],
            "quantity": item["quantity"],
            "category": item["category"],
            "expiry_date": db.effective_date(item),
            "printed_date": item["expiry_date"],
            "location": item.get("location"),
            "count": item.get("count", 1),
            "days_left": db.days_left_for(item),
            "barcode": item["barcode"],
        },
    }


def _summary_state(items_with_buckets: list[tuple[dict, str]]) -> dict:
    return {
        "state": str(len(items_with_buckets)),
        "attributes": {
            "friendly_name": "Pantry Tracker",
            "icon": "mdi:fridge-outline",
            "expiring_soon": sum(1 for _, b in items_with_buckets if b == db.BUCKET_EXPIRING_SOON),
            "expired": sum(1 for _, b in items_with_buckets if b == db.BUCKET_EXPIRED),
            "needs_date": sum(1 for _, b in items_with_buckets if b == db.BUCKET_UNKNOWN),
            "frozen": sum(1 for _, b in items_with_buckets if b == db.BUCKET_FROZEN),
            "items": [
                {
                    "entity_id": entity_id_for(item),
                    "name": item["name"],
                    "brand": item["brand"],
                    "expiry_date": db.effective_date(item),
                    "printed_date": item["expiry_date"],
                    "location": item.get("location"),
                    "count": item.get("count", 1),
                    "days_left": db.days_left_for(item),
                    "status": bucket,
                }
                for item, bucket in items_with_buckets
            ],
        },
    }


def sync(active_items: list[dict]) -> None:
    """Pushes the current item list to Home Assistant as sensor state.
    Safe to call often - after every dashboard mutation as well as every
    scheduled check - since it's just a handful of small state writes."""
    if not configured():
        return

    notify_days_before = settings.get_int("notify_days_before")
    items_with_buckets = [(item, db.bucket_for(item, notify_days_before)) for item in active_items]

    if not _request("POST", f"/states/{_SUMMARY_ENTITY_ID}", _summary_state(items_with_buckets)):
        return

    current_ids = set()
    for item, bucket in items_with_buckets:
        entity_id = entity_id_for(item)
        current_ids.add(entity_id)
        _request("POST", f"/states/{entity_id}", _item_state(item, bucket))

    previous_ids = set(json.loads(db.get_state(_SYNCED_IDS_STATE_KEY, "[]")))
    for stale_id in previous_ids - current_ids:
        _request("DELETE", f"/states/{stale_id}")

    db.set_state(_SYNCED_IDS_STATE_KEY, json.dumps(sorted(current_ids)))


def _pantry_panel_path() -> str:
    """This add-on's sidebar panel, on its Pantry tab. Home Assistant
    registers the panel at /<slug>, and passes anything after that to the
    add-on as its route (see the home-assistant/properties handling in
    index.html). Supervisor sets the container hostname to the slug with
    underscores swapped for hyphens, so this needs no extra permissions.
    """
    slug = socket.gethostname().replace("-", "_")
    return f"/{slug}/pantry"


def _due_for_notification(item: dict, bucket: str) -> bool:
    last_bucket = item.get("last_notified_bucket")
    last_at = item.get("last_notified_at")
    snoozed = item.get("snoozed_until")
    if last_bucket != bucket or not last_at:
        # Either never notified, or the item just moved into a worse bucket
        # (e.g. expiring_soon -> expired) - always worth a fresh alert.
        return True
    if snoozed and snoozed >= db.today().isoformat():
        return False
    repeat_hours = settings.get_int("notify_repeat_hours")
    elapsed = datetime.now(timezone.utc) - datetime.fromisoformat(last_at)
    return elapsed >= timedelta(hours=repeat_hours) - timedelta(hours=1)


def notify_summary(items: list[dict]) -> str:
    service = settings.notify_service()
    if not configured():
        return "not_configured"
    if not service or "." not in service:
        return "not_configured"
    due = []
    for item in items:
        bucket = db.bucket_for(item, settings.get_int("notify_days_before"))
        if bucket in _NOTIFIABLE_BUCKETS and _due_for_notification(item, bucket):
            due.append((item, bucket))
    if not due:
        return "nothing_due"
    grouped = {"Expired": [], "Today": [], "Tomorrow": []}
    for item, bucket in due:
        days = db.days_left_for(item)
        if bucket == db.BUCKET_EXPIRED:
            label = "Expired"
        elif days == 0:
            label = "Today"
        elif days == 1:
            label = "Tomorrow"
        else:
            label = f"In {days} days"
        name = item["name"] + (f" ×{item.get('count', 1)}" if item.get("count", 1) > 1 else "")
        grouped.setdefault(label, []).append(name)
    order = {"Expired": 0, "Today": 1, "Tomorrow": 2}
    labels = sorted(grouped, key=lambda label: (order.get(label, 3), int(label[3:-5]) if label.startswith("In ") else 0))
    message = " ".join(f"{label}: {', '.join(grouped[label])}." for label in labels if grouped[label])
    path = _pantry_panel_path()
    ids = [str(item["id"]) for item, _ in due]
    if len(ids) == 1:
        actions = [{"action": f"PANTRY_EATEN_{ids[0]}", "title": "Eaten"}]
        if due[0][1] == db.BUCKET_EXPIRING_SOON:
            actions.append({"action": f"PANTRY_FREEZE_{ids[0]}", "title": "Freeze"})
        else:
            actions.append({"action": f"PANTRY_BINNED_{ids[0]}", "title": "Binned"})
        actions.append({"action": f"PANTRY_SNOOZE_{','.join(ids)}", "title": "Snooze"})
    else:
        actions = [{"action": f"PANTRY_SNOOZE_{','.join(ids)}", "title": "Snooze"}]
    domain, _, name = service.partition(".")
    ok = _request("POST", f"/services/{domain}/{name}", {"title": f"{len(due)} item{'s' if len(due) != 1 else ''} to use",
        "message": message, "data": {"tag": "pantry-summary", "clickAction": path, "url": path, "actions": actions}})
    if ok:
        for item, bucket in due:
            db.mark_notified(item["id"], bucket)
    return "sent" if ok else "failed"
