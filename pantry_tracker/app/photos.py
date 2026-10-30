"""Normalises a product photo taken in the app before it's stored."""

from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError

# Only ever shown as a small thumbnail, so there's no value in keeping a
# full camera frame on disk for every product.
_MAX_DIMENSION = 800


def normalise_product_photo(image_bytes: bytes) -> bytes | None:
    """An upright, downscaled JPEG, or None if this isn't a readable image."""
    try:
        image = Image.open(BytesIO(image_bytes))
        image = ImageOps.exif_transpose(image)
        image.thumbnail((_MAX_DIMENSION, _MAX_DIMENSION))
        buffer = BytesIO()
        image.convert("RGB").save(buffer, format="JPEG", quality=85)
    except (UnidentifiedImageError, OSError, ValueError):
        return None
    return buffer.getvalue()
