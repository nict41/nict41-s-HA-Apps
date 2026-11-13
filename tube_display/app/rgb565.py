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


# Per-channel lookup tables for the conversions below. Each RGB565 byte, and each channel
# rebuilt from them, is two channels' bits that don't overlap, so ImageChops.add (which
# clips at 255, never reached here) combines them like a bitwise or: whole images at C speed
# instead of a Python loop per pixel.
_HI_R = [v & 0xF8 for v in range(256)]                      # red's top 5 bits
_HI_G = [v >> 5 for v in range(256)]                        # green's top 3 bits
_LO_G = [(v & 0x1C) << 3 for v in range(256)]               # green's next 3 bits
_LO_B = [v >> 3 for v in range(256)]                        # blue's top 5 bits
_R_HI = [(v & 0xF8) | (v >> 5) for v in range(256)]         # red, bit-replicated, from hi
_G_HI = [((v & 7) << 5) | ((v & 7) >> 1) for v in range(256)]  # green's bits from hi
_G_LO = [(v >> 5) << 2 for v in range(256)]                 # green's bits from lo
_B_LO = [((v & 0x1F) << 3) | ((v & 0x1F) >> 2) for v in range(256)]  # blue, bit-replicated


def image_to_rgb565(img) -> bytes:
    """Convert an RGB(A) image to raw RGB565 big-endian bytes."""
    _require_pil()
    from PIL import ImageChops
    r, g, b = img.convert("RGB").split()
    hi = ImageChops.add(r.point(_HI_R), g.point(_HI_G))
    lo = ImageChops.add(g.point(_LO_G), b.point(_LO_B))
    return Image.merge("LA", (hi, lo)).tobytes()            # hi, lo, hi, lo, ...


def rgb565_to_image(data: bytes, width: int, height: int):
    """Rebuild the exact RGB image the RGB565 bytes encode (the inverse of
    image_to_rgb565: each channel is expanded back with bit replication, so
    converting the result again gives the same bytes - round-trip exact)."""
    _require_pil()
    from PIL import ImageChops
    if len(data) != width * height * 2:
        raise ValueError(f"rgb565 body must be {width * height * 2} bytes, got {len(data)}")
    hi, lo = Image.frombytes("LA", (width, height), bytes(data)).split()
    r = hi.point(_R_HI)
    g = ImageChops.add(hi.point(_G_HI), lo.point(_G_LO))
    b = lo.point(_B_LO)
    return Image.merge("RGB", (r, g, b))


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
