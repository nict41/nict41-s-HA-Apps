"""Track123 tracking lookups (https://docs.track123.com).

A second, independently-quota'd provider alongside 17track. Useful because
17track's free allowance is now a one-time pool of numbers rather than a
recurring monthly quota, while Track123's free tier renews monthly -
configuring both lets registrations draw from whichever has room. Numbers
are registered without a courier code, so Track123 always auto-detects the
carrier from the number's own format - including for the LP/JJD-prefixed
Cainiao/AliExpress Standard Shipping numbers, where guessing a specific
courier code ourselves (cainiao vs. aliexpress) was a worse bet than
letting Track123's own detection pick whichever it actually resolves the
number under.
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


def _rejection_reasons(response: dict) -> dict[str, str]:
    """Alongside `accepted`, Track123 can return a `rejected` array for
    numbers it couldn't register or find, each with an error code/message -
    otherwise indistinguishable from a number it simply has no data for yet.
    Surfaced so a permanently-rejected number doesn't just sit unexplained."""
    rejected = (response.get("data") or {}).get("rejected") or response.get("rejected") or []
    reasons: dict[str, str] = {}
    for entry in rejected:
        number = entry.get("trackNo") or entry.get("number")
        if not number:
            continue
        error = entry.get("error") or {}
        code = error.get("code") or entry.get("code")
        msg = error.get("msg") or error.get("message") or entry.get("msg")
        if code and msg:
            reasons[number] = f"{code}: {msg}"
        elif msg or code:
            reasons[number] = msg or code
    return reasons


def register(parcels: list[tuple[str, str | None]]) -> None:
    """Register (tracking_number, carrier_name) pairs for tracking. Safe to
    call repeatedly - Track123 no-ops on numbers it's already tracking.

    carrier_name is accepted only for call-site symmetry with the 17track
    provider - it's deliberately ignored here, since auto-detect is what we
    want for every carrier (see module docstring)."""
    if not API_KEY or not parcels:
        return
    for chunk in _chunks(parcels, _MAX_NUMBERS_PER_REQUEST):
        payload = [{"trackNo": number} for number, _carrier_name in chunk]
        response = _post("tk/v2.1/track/import", payload)
        if response:
            for number, reason in _rejection_reasons(response).items():
                print(f"[parcel_tracker] Track123 declined to register '{number}': {reason}")
        time.sleep(_REQUEST_DELAY_SECONDS)


def _logistics_leg(entry: dict) -> dict:
    """A cross-border parcel (e.g. AliExpress/Cainiao) is first handled by
    an international leg (`localLogisticsInfo`), then handed off to a local
    last-mile courier once it reaches the destination country -
    `lastMileInfo.openApiWayBillInfo` mirrors `localLogisticsInfo`'s shape
    but covers just that final leg, and only appears once the handoff has
    actually happened. It holds the freshest events when present; relying
    on `localLogisticsInfo` alone goes stale the moment a parcel moves past
    the international leg it covers, even though the destination carrier's
    own tracking page keeps showing new events."""
    last_mile = (entry.get("lastMileInfo") or {}).get("openApiWayBillInfo") or {}
    if last_mile.get("trackingDetails"):
        return last_mile
    return entry.get("localLogisticsInfo") or {}


def _map_status(entry: dict, leg: dict) -> tuple[str, str | None, str | None]:
    status = _STATUS_MAP.get(entry.get("transitStatus") or "", "in_transit")

    details = leg.get("trackingDetails") or []
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
        reasons = _rejection_reasons(response)
        for number, reason in reasons.items():
            print(f"[parcel_tracker] Track123 rejected '{number}': {reason}")

        for number in chunk:
            entry = by_number.get(number)
            if entry is None and number not in reasons:
                print(f"[parcel_tracker] Track123 query for '{number}' returned no accepted or rejected entry")
            entry = entry or {}
            leg = _logistics_leg(entry)
            if entry and not leg.get("trackingDetails"):
                print(f"[parcel_tracker] Track123 accepted '{number}' but its response had no tracking events yet")
            status, detail, event_time = _map_status(entry, leg)
            carrier_name = leg.get("courierNameEN") or None
            if not detail and number in reasons:
                detail = f"Track123: {reasons[number]}"
            results[number] = {
                "status": status,
                "status_detail": detail,
                "last_event_time": event_time,
                "estimated_delivery": entry.get("expectedDelivery"),
                "carrier_name": carrier_name,
                # "Recognized" requires *both* a detected courier and an
                # actual movement event, not just one or the other - Track123
                # will sometimes guess a courier from a number's shape alone
                # (e.g. a phone number that happens to match a carrier's
                # number-length pattern) with no real tracking data behind
                # it, which would otherwise let exactly the kind of bogus
                # number auto-dismiss exists to clean up count as permanently
                # confirmed instead.
                "confirmed": bool(carrier_name) and bool(event_time),
            }

    return results
