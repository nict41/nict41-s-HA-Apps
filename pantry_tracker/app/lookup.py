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


def lookup_barcode(barcode: str) -> dict | None:
    """The product's name/brand/quantity/category/image, or None if it
    isn't in the database or the request failed. A miss is never fatal to
    the scan flow - the caller falls back to manual entry either way."""
    url = f"{_BASE_URL}/{barcode}.json"
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            payload = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError):
        return None

    if payload.get("status") != 1:
        return None

    product = payload.get("product") or {}
    return {
        "name": product.get("product_name") or product.get("product_name_en") or "",
        "brand": (product.get("brands") or "").split(",")[0].strip(),
        "quantity": product.get("quantity") or "",
        "category": (product.get("categories") or "").split(",")[0].strip(),
        "image_url": product.get("image_front_small_url") or product.get("image_front_url") or "",
    }
