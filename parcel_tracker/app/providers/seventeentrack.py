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


_REDACTED = "<redacted>"

# Set by _post() right after each HTTP call (success or failure) - holds the
# redacted request and the response. register()/get_track_info() copy it out
# immediately via _capture() and attribute it to the numbers in the chunk
# that was just posted, before the next _post() call overwrites it.
_last_exchange: dict | None = None

# Per-number diagnostics, keyed by stage ("register" / "get_track_info").
# Read by main.py right after each provider call and persisted to the DB -
# this in-memory cache only needs to survive that long.
_raw_exchanges: dict[str, dict[str, dict]] = {}


def _redact_headers(headers: dict) -> dict:
    return {k: (_REDACTED if k.lower() == "17token" else v) for k, v in headers.items()}


def _post(path: str, payload: list[dict]) -> dict | None:
    global _last_exchange
    if not API_KEY:
        return None
    headers = {"Content-Type": "application/json", "17token": API_KEY}
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(f"{_BASE_URL}/{path}", data=body, method="POST", headers=headers)
    request_record = {
        "method": "POST",
        "url": req.full_url,
        "headers": _redact_headers(headers),
        "body": payload,
    }
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            parsed = json.loads(resp.read().decode("utf-8"))
            _last_exchange = {"request": request_record, "response": {"status": resp.status, "body": parsed}}
            return parsed
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
        _last_exchange = {
            "request": request_record,
            "response": {"status": getattr(exc, "code", None), "body": str(exc)},
        }
        print(f"[parcel_tracker] 17track request to '{path}' failed: {exc}")
        return None


def _capture(stage: str, numbers, exchange: dict | None) -> None:
    if exchange is None:
        return
    stamped = {**exchange, "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    for number in numbers:
        _raw_exchanges.setdefault(number, {})[stage] = stamped


def get_raw_exchange(number: str) -> dict | None:
    """The most recently captured register()/get_track_info() exchange(s)
    for a tracking number, or None if it's never had one. Read by main.py
    right after a register()/get_track_info() call to persist into the DB."""
    return _raw_exchanges.get(number)


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def register(parcels: list[tuple[str, str | None]]) -> None:
    """Register (tracking_number, carrier_name) pairs for tracking. Safe to
    call repeatedly - 17track no-ops on numbers it's already tracking.

    carrier_name is accepted only for call-site symmetry with the Track123
    provider - it's deliberately ignored here, since carrier 0 (auto-detect)
    is the whole point for the cross-border numbers this matters most for
    (see module docstring)."""
    if not API_KEY or not parcels:
        return
    for chunk in _chunks(parcels, _MAX_NUMBERS_PER_REQUEST):
        numbers = [number for number, _ in chunk]
        _post("register", [{"number": number, "carrier": 0} for number, _ in chunk])
        _capture("register", numbers, _last_exchange)
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


def _provider_events(track_info: dict) -> list[dict]:
    """The full journey, newest first (the order providers[].events is
    already in) - latest_event above only ever surfaces the single latest
    entry, dropping everything before it."""
    providers = (track_info.get("tracking") or {}).get("providers") or []
    if not providers:
        return []
    events = []
    for event in providers[0].get("events") or []:
        time = event.get("time_iso") or event.get("time_utc") or event.get("time_raw")
        detail = event.get("description")
        if not time and not detail:
            continue
        events.append({"time": time, "detail": detail, "location": event.get("location") or None})
    return events


def get_track_info(tracking_numbers: list[str]) -> dict[str, dict]:
    """Returns {tracking_number: {"status", "status_detail", "last_event_time",
    "estimated_delivery", "carrier_name", "confirmed", "events"}}.

    "events" is the full journey (newest first), each a {"time", "detail",
    "location"} dict - everything providers[].events has, not just the
    single latest entry the other fields above are derived from.

    A number is only left out of the result if the whole request failed
    (network/auth error) - a request that succeeded but doesn't recognise a
    given number still gets an entry, with confirmed=False, so callers can
    tell "17track has never identified this as a real tracking number" apart
    from "we couldn't ask 17track this time". That distinction is what
    drives auto-dismissing candidates 17track never confirms."""
    results: dict[str, dict] = {}
    if not API_KEY or not tracking_numbers:
        return results

    for chunk in _chunks(tracking_numbers, _MAX_NUMBERS_PER_REQUEST):
        response = _post("gettrackinfo", [{"number": n, "carrier": 0} for n in chunk])
        _capture("get_track_info", chunk, _last_exchange)
        time.sleep(_REQUEST_DELAY_SECONDS)
        if response is None:
            continue

        accepted = (response.get("data") or {}).get("accepted") or []
        by_number = {entry.get("number"): entry for entry in accepted if entry.get("number")}

        for number in chunk:
            entry = by_number.get(number)
            if entry is None:
                print(f"[parcel_tracker] 17track query for '{number}' returned no accepted entry")
            track_info = (entry or {}).get("track_info") or {}
            if entry and not track_info.get("latest_event"):
                print(f"[parcel_tracker] 17track accepted '{number}' but its response had no tracking events yet")
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
                "events": _provider_events(track_info),
                # "Recognized" requires *both* a matched carrier and an
                # actual movement event, not just one or the other - 17track
                # will sometimes match a number to a carrier from its shape
                # alone (e.g. a phone number that happens to match a
                # carrier's number-length pattern) with no real tracking data
                # behind it, which would otherwise let exactly the kind of
                # bogus number auto-dismiss exists to clean up count as
                # permanently confirmed instead.
                "confirmed": bool(carrier_name) and bool(event_time),
            }

    return results
