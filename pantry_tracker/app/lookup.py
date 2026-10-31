"""Looks up a product barcode against Open Food Facts
(https://world.openfoodfacts.org) - a free, keyless, crowd-sourced product
database with broad coverage of supermarket packaging worldwide. No API
key, account, or configuration is needed, unlike every commercial
barcode-lookup service with comparable coverage.
"""

import json
import urllib.error
import urllib.request

_BASE_URL = "https://world.openfoodfacts.org/api/v2/product"
# Open Food Facts' API guidelines ask every client to identify itself with a
# descriptive User-Agent, so abusive clients can be identified and blocked
# without penalising everyone else.
_USER_AGENT = "PantryTracker-HomeAssistantApp/1.0 (+https://github.com/nict41/nict41-s-HA-Apps)"
_TIMEOUT_SECONDS = 10


class LookupUnavailable(Exception):
    """Open Food Facts could not be reached or returned an invalid response."""


def lookup_barcode(barcode: str) -> dict | None:
    """The product fields, or None when Open Food Facts reports no match."""
    url = f"{_BASE_URL}/{barcode}.json"
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise LookupUnavailable(f"Open Food Facts returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError, OSError) as exc:
        raise LookupUnavailable("Open Food Facts request failed") from exc

    if not isinstance(payload, dict):
        raise LookupUnavailable("Open Food Facts returned invalid JSON")
    status = payload.get("status")
    if type(status) is int and status == 0:
        return None
    if type(status) is not int or status != 1:
        raise LookupUnavailable("Open Food Facts returned an unknown status")

    product = payload.get("product") or {}
    if not isinstance(product, dict):
        raise LookupUnavailable("Open Food Facts returned invalid product data")
    return {
        "name": product.get("product_name_en") or product.get("product_name") or "",
        "brand": (product.get("brands") or "").split(",")[0].strip(),
        "quantity": product.get("quantity") or "",
        "category": (product.get("categories") or "").split(",")[0].strip(),
        "image_url": product.get("image_front_small_url") or product.get("image_front_url") or "",
    }
