"""Decodes a product barcode from an uploaded photo.

Uses pyzbar (a ctypes wrapper around the zbar C library, installed via the
Alpine `zbar` package) rather than a pure-Python decoder - reading a
barcode from a real phone photo (skew, blur, uneven lighting, a messy
kitchen in the background) needs zbar's actual image-processing pipeline,
not a toy implementation.
"""

from io import BytesIO

from PIL import Image, ImageOps
from pyzbar.pyzbar import decode as _zbar_decode

# Product barcodes seen on supermarket packaging. Other symbologies zbar
# supports (QR, Code128, ...) are deliberately excluded, so an unrelated
# barcode or QR code elsewhere on the packaging is never mistaken for the
# product code.
_PRODUCT_SYMBOLS = {"EAN13", "EAN8", "UPCA", "UPCE"}

# Phone photos routinely arrive well above 4000px on the long edge,
# which is far more detail than zbar needs and makes decoding noticeably
# slower. Downscaling first keeps it well under a second.
_MAX_DIMENSION = 1600


def decode_barcode(image_bytes: bytes) -> str | None:
    """Returns the first recognised product barcode's digits, or None if no
    photo could be read or no product barcode was found in it."""
    try:
        image = Image.open(BytesIO(image_bytes))
        # Phone cameras store orientation as EXIF metadata rather than
        # rotating the pixels themselves; without this a sideways photo
        # decodes fine (zbar doesn't care about orientation) but any
        # downstream use of the image would show it rotated.
        image = ImageOps.exif_transpose(image)
    except Exception:
        return None

    image.thumbnail((_MAX_DIMENSION, _MAX_DIMENSION))
    grayscale = image.convert("L")

    for symbol in _zbar_decode(grayscale):
        if symbol.type in _PRODUCT_SYMBOLS:
            return symbol.data.decode("ascii", errors="ignore")
    return None
