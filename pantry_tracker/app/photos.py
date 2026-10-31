"""Normalises a product photo taken in the app before it's stored."""

from io import BytesIO
import urllib.parse
import urllib.request

from PIL import Image, ImageOps, UnidentifiedImageError

import db

# Only ever shown as a small thumbnail, so there's no value in keeping a
# full camera frame on disk for every product.
_MAX_DIMENSION = 800


def normalise_product_photo(image_bytes: bytes, max_dimension: int = _MAX_DIMENSION) -> bytes | None:
    """An upright, downscaled JPEG, or None if this isn't a readable image."""
    try:
        image = Image.open(BytesIO(image_bytes))
        image = ImageOps.exif_transpose(image)
        image.thumbnail((max_dimension, max_dimension))
        buffer = BytesIO()
        image.convert("RGB").save(buffer, format="JPEG", quality=85)
    except (UnidentifiedImageError, OSError, ValueError):
        return None
    return buffer.getvalue()


_PRODUCT_IMAGE_MAX_BYTES = 5 * 1024 * 1024
_PRODUCT_IMAGE_TIMEOUT_SECONDS = 10


def download_product_image(image_url: str, barcode: str) -> bool:
    """Downloads and stores an allowed Open Food Facts image, best effort."""
    try:
        parsed = urllib.parse.urlparse(image_url)
        host = (parsed.hostname or "").lower()
        if (parsed.scheme != "https"
                or not (host == "images.openfoodfacts.org" or host.endswith(".images.openfoodfacts.org"))
                or not db.valid_barcode(barcode)):
            return False
        if db.product_image_path(barcode).exists():
            return True
        req = urllib.request.Request(image_url, headers={"User-Agent": "PantryTracker-HomeAssistantApp/1.0"})
        with urllib.request.urlopen(req, timeout=_PRODUCT_IMAGE_TIMEOUT_SECONDS) as response:
            payload = response.read(_PRODUCT_IMAGE_MAX_BYTES)
        if len(payload) >= _PRODUCT_IMAGE_MAX_BYTES:
            return False
        normalised = normalise_product_photo(payload, max_dimension=400)
        if normalised is None:
            return False
        db.save_product_image(barcode, normalised)
        return True
    except Exception:
        return False
