"""17track.net status lookups.

Registers and queries every tracking number with carrier code 0
(auto-detect) rather than a specific carrier code, since AliExpress/eBay
cross-border shipments are fragmented across dozens of regional carriers
(Cainiao, 4PX, YunExpress, Yanwen, ...) that 17track is specifically built
to resolve from the number's own format - guessing a specific carrier code
would defeat that.

The exact v2.2 response shape is the official contract, but field-level
behavior (e.g. which status sub-codes a given carrier actually emits) can
vary by carrier in practice, so status interpretation here is intentionally
defensive: unrecognized codes fall back to "in_transit" rather than
raising, and any request failure degrades to "no data" instead of
breaking the rest of the app.
"""

import json
import os
import time
import urllib.error
import urllib.request

API_KEY = os.environ.get("SEVENTEENTRACK_API_KEY", "").strip()

_BASE_URL = "https://api.17track.net/track/v2.2"
_MAX_NUMBERS_PER_REQUEST = 40
_REQUEST_DELAY_SECONDS = 0.4  # stays under the documented 3 req/sec cap

# 17track's coarse top-level status categories. Anything not listed here
# (including codes added to the API after this was written) falls back to
# "in_transit" in _map_status() rather than being treated as an error.
_STATUS_MAP = {
    "InfoReceived": "in_transit",
    "InTransit": "in_transit",
    "Expired": "exception",
    "AvailableForPickup": "in_transit",
    "OutForDelivery": "in_transit",
    "DeliveryFailure": "exception",
    "Delivered": "delivered",
    "Exception": "exception",
    "NotFound": "exception",
}


def configured() -> bool:
    return bool(API_KEY)


def _post(path: str, payload: list[dict]) -> dict | None:
    if not API_KEY:
        return None
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{_BASE_URL}/{path}",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "17token": API_KEY},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
        print(f"[parcel_tracker] 17track request to '{path}' failed: {exc}")
        return None


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def register(tracking_numbers: list[str]) -> None:
    """Register numbers for tracking. Safe to call repeatedly - 17track
    no-ops on numbers it's already tracking."""
    if not API_KEY or not tracking_numbers:
        return
    for chunk in _chunks(tracking_numbers, _MAX_NUMBERS_PER_REQUEST):
        _post("register", [{"number": n, "carrier": 0} for n in chunk])
        time.sleep(_REQUEST_DELAY_SECONDS)


def _map_status(track_info: dict) -> tuple[str, str | None, str | None]:
    latest_status = (track_info.get("latest_status") or {}).get("status", "")
    status = _STATUS_MAP.get(latest_status, "in_transit")

    latest_event = track_info.get("latest_event") or {}
    detail = latest_event.get("description") or latest_event.get("status_description")
    event_time = latest_event.get("time_iso") or latest_event.get("time_utc")

    return status, detail, event_time


def _detected_carrier_name(track_info: dict) -> str | None:
    """`tracking.providers` holds the carrier(s) 17track actually matched
    the number to, as opposed to the carrier code (0/auto-detect) we
    registered it with."""
    providers = (track_info.get("tracking") or {}).get("providers") or []
    if not providers:
        return None
    return (providers[0].get("provider") or {}).get("name") or None


def get_track_info(tracking_numbers: list[str]) -> dict[str, dict]:
    """Returns {tracking_number: {"status", "status_detail", "last_event_time",
    "estimated_delivery", "carrier_name", "confirmed"}}. Numbers with no data
    back from the API are omitted from the result rather than guessed at."""
    results: dict[str, dict] = {}
    if not API_KEY or not tracking_numbers:
        return results

    for chunk in _chunks(tracking_numbers, _MAX_NUMBERS_PER_REQUEST):
        response = _post("gettrackinfo", [{"number": n, "carrier": 0} for n in chunk])
        time.sleep(_REQUEST_DELAY_SECONDS)
        if not response:
            continue

        accepted = (response.get("data") or {}).get("accepted") or []
        for entry in accepted:
            number = entry.get("number")
            track_info = entry.get("track_info") or {}
            if not number or not track_info:
                continue
            status, detail, event_time = _map_status(track_info)
            estimated_delivery = (track_info.get("time_metrics") or {}).get(
                "estimated_delivery_date", {}
            ).get("from")
            carrier_name = _detected_carrier_name(track_info)
            results[number] = {
                "status": status,
                "status_detail": detail,
                "last_event_time": event_time,
                "estimated_delivery": estimated_delivery,
                "carrier_name": carrier_name,
                # "Recognized" = 17track matched the number to a real carrier
                # or returned an actual movement event, as opposed to merely
                # echoing back a number it has no data for. Used to auto-confirm
                # pending candidates, a far stronger signal than our own
                # pattern-based carrier guess.
                "confirmed": bool(carrier_name) or bool(event_time),
            }

    return results
