"""Track123 tracking lookups (https://docs.track123.com).

A second, independently-quota'd provider alongside 17track. Useful because
17track's free allowance is now a one-time pool of numbers rather than a
recurring monthly quota, while Track123's free tier renews monthly -
configuring both lets registrations draw from whichever has room. Like the
17track module, numbers are registered without a courier code so Track123
auto-detects the actual carrier from the number's own format.
"""

import json
import os
import time
import urllib.error
import urllib.request

API_KEY = os.environ.get("TRACK123_API_KEY", "").strip()

_BASE_URL = "https://api.track123.com/gateway/open-api"
_MAX_NUMBERS_PER_REQUEST = 40
_REQUEST_DELAY_SECONDS = 0.25  # stays under the documented 5 req/sec cap

# Track123's transitStatus enum. Anything not listed here (including codes
# added to the API after this was written) falls back to "in_transit" in
# _map_status() rather than being treated as an error.
_STATUS_MAP = {
    "INIT": "in_transit",
    "NO_RECORD": "in_transit",
    "INFO_RECEIVED": "in_transit",
    "IN_TRANSIT": "in_transit",
    "WAITING_DELIVERY": "in_transit",
    "DELIVERY_FAILED": "exception",
    "ABNORMAL": "exception",
    "DELIVERED": "delivered",
    "EXPIRED": "exception",
}


def configured() -> bool:
    return bool(API_KEY)


def _post(path: str, payload) -> dict | None:
    if not API_KEY:
        return None
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{_BASE_URL}/{path}",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Track123-Api-Secret": API_KEY},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
        print(f"[parcel_tracker] Track123 request to '{path}' failed: {exc}")
        return None


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def register(tracking_numbers: list[str]) -> None:
    """Register numbers for tracking. Safe to call repeatedly - Track123
    no-ops on numbers it's already tracking."""
    if not API_KEY or not tracking_numbers:
        return
    for chunk in _chunks(tracking_numbers, _MAX_NUMBERS_PER_REQUEST):
        _post("tk/v2.1/track/import", [{"trackNo": n} for n in chunk])
        time.sleep(_REQUEST_DELAY_SECONDS)


def _map_status(entry: dict) -> tuple[str, str | None, str | None]:
    status = _STATUS_MAP.get(entry.get("transitStatus") or "", "in_transit")

    details = (entry.get("localLogisticsInfo") or {}).get("trackingDetails") or []
    latest = details[0] if details else {}
    detail = latest.get("eventDetail")
    event_time = latest.get("eventTime")

    return status, detail, event_time


def get_track_info(tracking_numbers: list[str]) -> dict[str, dict]:
    """Returns {tracking_number: {"status", "status_detail", "last_event_time",
    "estimated_delivery", "carrier_name", "confirmed"}}.

    A number is only left out of the result if the whole request failed
    (network/auth error) - a request that succeeded but doesn't recognise a
    given number still gets an entry, with confirmed=False, so callers can
    tell "Track123 has never identified this as a real tracking number" apart
    from "we couldn't ask Track123 this time". That distinction is what
    drives auto-dismissing candidates Track123 never confirms."""
    results: dict[str, dict] = {}
    if not API_KEY or not tracking_numbers:
        return results

    for chunk in _chunks(tracking_numbers, _MAX_NUMBERS_PER_REQUEST):
        response = _post(
            "tk/v2.1/track/query",
            {"trackNoInfos": [{"trackNo": n} for n in chunk], "queryPageSize": len(chunk)},
        )
        time.sleep(_REQUEST_DELAY_SECONDS)
        if response is None:
            continue

        accepted = ((response.get("data") or {}).get("accepted") or {}).get("content") or []
        by_number = {entry.get("trackNo"): entry for entry in accepted if entry.get("trackNo")}

        for number in chunk:
            entry = by_number.get(number) or {}
            status, detail, event_time = _map_status(entry)
            carrier_name = (entry.get("localLogisticsInfo") or {}).get("courierNameEN") or None
            results[number] = {
                "status": status,
                "status_detail": detail,
                "last_event_time": event_time,
                "estimated_delivery": entry.get("expectedDelivery"),
                "carrier_name": carrier_name,
                # "Recognized" = Track123 identified the courier or returned an
                # actual movement event, as opposed to merely echoing back a
                # number with no record. Used to auto-confirm pending
                # candidates, a far stronger signal than our own pattern-based
                # carrier guess.
                "confirmed": bool(carrier_name) or bool(event_time),
            }

    return results
