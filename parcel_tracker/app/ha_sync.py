"""Exposes tracked parcels to Home Assistant as sensor entities, using the
add-on's own Supervisor-granted access to the Home Assistant Core API
(`homeassistant_api: true` in config.yaml) rather than requiring the user to
set up MQTT or any extra credentials.

One `sensor.parcel_tracker_summary` entity carries the full parcel list as an
attribute, for the companion Lovelace card to read directly via `hass.states`
without needing network access to the add-on itself (ingress URLs aren't
reachable from a normal dashboard card). One `sensor.parcel_tracker_<slug>`
entity per non-archived parcel exists so automations can trigger on a
specific package's state (e.g. "notify me when sensor.parcel_tracker_xxx
becomes delivered") - a single attribute on the summary entity isn't usable
as a state-trigger target in the automation UI.

States set this way aren't registered with any integration, so they don't
survive a Home Assistant restart on their own - but since every sync cycle
re-sets them, they reappear within one `poll_interval_minutes` of HA coming
back up.
"""

import json
import os
import re
import urllib.error
import urllib.request

import carriers
import db

SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "").strip()

_BASE_URL = "http://supervisor/core/api"
_ENTITY_PREFIX = "sensor.parcel_tracker_"
_SUMMARY_ENTITY_ID = "sensor.parcel_tracker_summary"
_SYNCED_IDS_STATE_KEY = "ha_synced_entity_ids"

_DEFAULT_ICON = "mdi:package-variant-closed"
_STATUS_ICONS = {
    db.STATUS_PENDING: "mdi:package-variant-closed",
    db.STATUS_ACTIVE: "mdi:truck-delivery",
    db.STATUS_EXCEPTION: "mdi:alert-circle",
    db.STATUS_DELIVERED: "mdi:check-circle",
}

# Parcels in these statuses no longer get an entity - any previously-synced
# entity for one is deleted instead, so dismissing/archiving/deleting a
# parcel cleans up after itself in Home Assistant too.
_EXCLUDED_STATUSES = {db.STATUS_ARCHIVED, db.STATUS_DISMISSED}

# The entity state (and the summary's per-parcel "status") use a simpler,
# stable vocabulary than the internal status column - documented in the
# README as the public contract automations are written against - so
# internal statuses can be renamed/added without breaking either one.
_EXTERNAL_STATUS = {
    db.STATUS_PENDING: "pending",
    db.STATUS_ACTIVE: "active",
    db.STATUS_EXCEPTION: "exception",
    db.STATUS_DELIVERED: "delivered",
}


def configured() -> bool:
    return bool(SUPERVISOR_TOKEN)


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")


def entity_id_for(parcel: dict) -> str:
    return f"{_ENTITY_PREFIX}{_slugify(parcel['tracking_number'])}"


def _request(method: str, entity_id: str, body: dict | None = None) -> None:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        f"{_BASE_URL}/states/{entity_id}",
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"[parcel_tracker] Home Assistant API request to '{entity_id}' failed: {exc}")


def _set_state(entity_id: str, state: dict) -> None:
    _request("POST", entity_id, state)


def _delete_state(entity_id: str) -> None:
    _request("DELETE", entity_id)


def _parcel_state(parcel: dict) -> dict:
    return {
        "state": _EXTERNAL_STATUS.get(parcel["status"], parcel["status"]),
        "attributes": {
            "friendly_name": parcel["description"] or parcel["tracking_number"],
            "icon": _STATUS_ICONS.get(parcel["status"], _DEFAULT_ICON),
            "tracking_number": parcel["tracking_number"],
            "carrier_name": parcel["carrier_name"],
            "description": parcel["description"],
            "status_detail": parcel["status_detail"],
            "estimated_delivery": parcel["estimated_delivery"],
            "last_event_time": parcel["last_event_time"],
            "delivered_at": parcel["delivered_at"],
            "confidence": parcel["confidence"],
            "tracking_provider": parcel["tracking_provider"],
        },
    }


def _summary_state(parcels: list[dict]) -> dict:
    return {
        "state": str(len(parcels)),
        "attributes": {
            "friendly_name": "Parcel Tracker",
            "icon": "mdi:truck-delivery",
            "pending_confirmation": sum(1 for p in parcels if p["status"] == db.STATUS_PENDING),
            "in_transit": sum(1 for p in parcels if p["status"] in (db.STATUS_ACTIVE, db.STATUS_EXCEPTION)),
            "delivered": sum(1 for p in parcels if p["status"] == db.STATUS_DELIVERED),
            "parcels": [
                {
                    "entity_id": entity_id_for(p),
                    "tracking_number": p["tracking_number"],
                    "carrier_name": p["carrier_name"],
                    "description": p["description"],
                    "status": _EXTERNAL_STATUS.get(p["status"], p["status"]),
                    "status_detail": p["status_detail"],
                    "estimated_delivery": p["estimated_delivery"],
                    "last_event_time": p["last_event_time"],
                    "delivered_at": p["delivered_at"],
                    "confidence": p["confidence"],
                    "tracking_provider": p["tracking_provider"],
                    "tracking_url": carriers.get_tracking_url(p["tracking_number"]),
                }
                for p in parcels
            ],
        },
    }


def sync(all_parcels: list[dict]) -> None:
    if not configured():
        return

    tracked = [p for p in all_parcels if p["status"] not in _EXCLUDED_STATUSES]
    _set_state(_SUMMARY_ENTITY_ID, _summary_state(tracked))

    current_ids = set()
    for parcel in tracked:
        entity_id = entity_id_for(parcel)
        current_ids.add(entity_id)
        _set_state(entity_id, _parcel_state(parcel))

    previous_ids = set(json.loads(db.get_state(_SYNCED_IDS_STATE_KEY, "[]")))
    for stale_id in previous_ids - current_ids:
        _delete_state(stale_id)

    db.set_state(_SYNCED_IDS_STATE_KEY, json.dumps(sorted(current_ids)))
