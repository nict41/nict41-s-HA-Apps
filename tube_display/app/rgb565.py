"""RGB565 conversion and splitting the 810x240 strip into six tube tiles.

The clock expects raw RGB565 big-endian pixels (high byte first) per tube,
135x240 by default, tubes 0..5 left to right.
"""

from __future__ import annotations

TUBE_W = 135
TUBE_H = 240
TUBE_COUNT = 6
STRIP_W = TUBE_W * TUBE_COUNT  # 810
STRIP_H = TUBE_H  # 240

try:
    from PIL import Image
except Exception:  # Pillow not importable (e.g. bare test env): constants still work
    Image = None


def _require_pil():
    if Image is None:
        raise RuntimeError("Pillow is required for image conversion")


def rgb565_bytes(r: int, g: int, b: int) -> bytes:
    """One RGB565 big-endian pixel (high byte first)."""
    v = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
    return bytes(((v >> 8) & 0xFF, v & 0xFF))


def image_to_rgb565(img) -> bytes:
    """Convert an RGB(A) image to raw RGB565 big-endian bytes."""
    _require_pil()
    img = img.convert("RGB")
    out = bytearray(img.width * img.height * 2)
    px = img.load()
    i = 0
    for y in range(img.height):
        for x in range(img.width):
            r, g, b = px[x, y][:3]
            v = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
            out[i] = (v >> 8) & 0xFF
            out[i + 1] = v & 0xFF
            i += 2
    return bytes(out)


def rgb565_to_image(data: bytes, width: int, height: int):
    """Rebuild the exact RGB image the RGB565 bytes encode (the inverse of
    image_to_rgb565: each channel is expanded back with bit replication, so
    converting the result again gives the same bytes - round-trip exact)."""
    _require_pil()
    if len(data) != width * height * 2:
        raise ValueError(f"rgb565 body must be {width * height * 2} bytes, got {len(data)}")
    img = Image.new("RGB", (width, height))
    px = img.load()
    i = 0
    for y in range(height):
        for x in range(width):
            v = (data[i] << 8) | data[i + 1]
            r = ((v >> 8) & 0xF8) | ((v >> 13) & 0x07)
            g = ((v >> 3) & 0xFC) | ((v >> 9) & 0x03)
            b = ((v & 0x1F) << 3) | ((v >> 2) & 0x07)
            px[x, y] = (r, g, b)
            i += 2
    return img


def strip_to_tiles(strip) -> list:
    """Split an 810x240 strip image into six 135x240 tube images, left to right."""
    _require_pil()
    strip = strip.convert("RGB")
    if strip.size != (STRIP_W, STRIP_H):
        strip = strip.resize((STRIP_W, STRIP_H))
    return [strip.crop((i * TUBE_W, 0, (i + 1) * TUBE_W, TUBE_H)) for i in range(TUBE_COUNT)]


def tile_to_rgb565(tile) -> bytes:
    """One 135x240 tile to RGB565 bytes (resized if needed)."""
    _require_pil()
    if tile.size != (TUBE_W, TUBE_H):
        tile = tile.resize((TUBE_W, TUBE_H))
    return image_to_rgb565(tile)


def strip_to_tube_bytes(strip) -> list[bytes]:
    """An 810x240 strip to six RGB565 payloads, one per tube."""
    return [tile_to_rgb565(t) for t in strip_to_tiles(strip)]
