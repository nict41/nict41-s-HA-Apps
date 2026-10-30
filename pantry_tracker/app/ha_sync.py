"""Exposes pantry items to Home Assistant as sensor entities, and sends a
direct notification through a configured `notify.*` service when an item
newly becomes due. Both use the add-on's own Supervisor-granted access to
the Home Assistant Core API (`homeassistant_api: true` in config.yaml), so
no MQTT setup or extra credentials are needed - the same pattern Parcel
Tracker uses elsewhere in this repo.

One `sensor.pantry_tracker_summary` entity carries the full item list as an
attribute. One `sensor.pantry_tracker_<id>_<name>` entity per active item
exists so automations can trigger on a specific item's state, the same
reasoning Parcel Tracker applies to per-parcel entities.

States set this way aren't registered with any integration, so they don't
survive a Home Assistant restart on their own - but since every check cycle
re-sets them, they reappear within `check_interval_hours` of HA coming back
up (sooner if the dashboard is used, since every item mutation re-syncs
immediately).
"""

import json
import os
import re
import socket
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
}

# Buckets worth alerting on. BUCKET_OK and BUCKET_UNKNOWN never notify - an
# item with no known expiry date has nothing to warn about yet.
_NOTIFIABLE_BUCKETS = (db.BUCKET_EXPIRING_SOON, db.BUCKET_EXPIRED)


def configured() -> bool:
    return bool(SUPERVISOR_TOKEN)


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
            "expiry_date": item["expiry_date"],
            "days_left": db.days_left_for(item["expiry_date"]),
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
            "items": [
                {
                    "entity_id": entity_id_for(item),
                    "name": item["name"],
                    "brand": item["brand"],
                    "expiry_date": item["expiry_date"],
                    "days_left": db.days_left_for(item["expiry_date"]),
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
    items_with_buckets = [(item, db.bucket_for(item["expiry_date"], notify_days_before)) for item in active_items]

    _request("POST", f"/states/{_SUMMARY_ENTITY_ID}", _summary_state(items_with_buckets))

    current_ids = set()
    for item, bucket in items_with_buckets:
        entity_id = entity_id_for(item)
        current_ids.add(entity_id)
        _request("POST", f"/states/{entity_id}", _item_state(item, bucket))

    previous_ids = set(json.loads(db.get_state(_SYNCED_IDS_STATE_KEY, "[]")))
    for stale_id in previous_ids - current_ids:
        _request("DELETE", f"/states/{stale_id}")

    db.set_state(_SYNCED_IDS_STATE_KEY, json.dumps(sorted(current_ids)))


def _notify_message(item: dict, bucket: str) -> tuple[str, str]:
    days_left = db.days_left_for(item["expiry_date"])
    label = f"{item['name']} ({item['brand']})" if item["brand"] else item["name"]
    if bucket == db.BUCKET_EXPIRED:
        overdue = abs(days_left) if days_left is not None else 0
        return (
            "Pantry item expired",
            f"{label} expired {overdue} day(s) ago ({item['expiry_date']}).",
        )
    return (
        "Pantry item expiring soon",
        f"{label} expires in {days_left} day(s) ({item['expiry_date']}).",
    )


def _ingress_click_action() -> str:
    """The stable sidebar path Home Assistant uses to open this add-on's
    own ingress panel, landing on its Pantry tab (see index.html's
    `#pantry` hash handling) - set as a notification's clickAction so
    tapping it opens straight there instead of wherever Home Assistant
    last was. Supervisor always names an add-on's own container after its
    slug, so reading our own hostname needs no extra permissions beyond
    what this add-on already has.
    """
    return f"/hassio/ingress/{socket.gethostname()}#pantry"


def _due_for_notification(item: dict, bucket: str) -> bool:
    last_bucket = item.get("last_notified_bucket")
    last_at = item.get("last_notified_at")
    if last_bucket != bucket or not last_at:
        # Either never notified, or the item just moved into a worse bucket
        # (e.g. expiring_soon -> expired) - always worth a fresh alert.
        return True
    repeat_hours = settings.get_int("notify_repeat_hours")
    elapsed = datetime.now(timezone.utc) - datetime.fromisoformat(last_at)
    return elapsed >= timedelta(hours=repeat_hours)


def notify_if_due(item: dict, bucket: str) -> bool:
    """Sends a notification through the configured `notify.*` service if
    this item is in an alertable bucket and is due one (new bucket, or a
    repeat reminder once `notify_repeat_hours` has passed). Returns whether
    a notification was sent."""
    service = settings.notify_service()
    if not service or "." not in service or bucket not in _NOTIFIABLE_BUCKETS:
        return False
    if not _due_for_notification(item, bucket):
        return False

    domain, _, service_name = service.partition(".")
    title, message = _notify_message(item, bucket)
    _request(
        "POST",
        f"/services/{domain}/{service_name}",
        {"title": title, "message": message, "data": {"clickAction": _ingress_click_action()}},
    )
    db.mark_notified(item["id"], bucket)
    return True
