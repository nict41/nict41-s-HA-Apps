"""RGB565 conversion and strip splitting (needs Pillow)."""

import pytest

from conftest import pil, requires_pil

pytestmark = requires_pil

if pil:
    from PIL import Image
    from rgb565 import (STRIP_H, STRIP_W, TUBE_COUNT, TUBE_H, TUBE_W,
                        image_to_rgb565, rgb565_bytes, strip_to_tiles,
                        strip_to_tube_bytes)


def test_rgb565_red():
    assert rgb565_bytes(255, 0, 0) == b"\xf8\x00"


def test_rgb565_blue():
    assert rgb565_bytes(0, 0, 255) == b"\x00\x1f"


def test_rgb565_white():
    assert rgb565_bytes(255, 255, 255) == b"\xff\xff"


def test_rgb565_black():
    assert rgb565_bytes(0, 0, 0) == b"\x00\x00"


def test_rgb565_green():
    # pure green: G6 = 0b111111 -> 0x07E0
    assert rgb565_bytes(0, 255, 0) == b"\x07\xe0"


def test_image_to_rgb565_size_and_content():
    img = Image.new("RGB", (4, 2), (255, 0, 0))
    data = image_to_rgb565(img)
    assert len(data) == 4 * 2 * 2
    assert data == b"\xf8\x00" * 8


def test_strip_split_into_six_tiles():
    # each tube a different colour so tiles are distinguishable
    strip = Image.new("RGB", (STRIP_W, STRIP_H))
    for i in range(TUBE_COUNT):
        for x in range(i * TUBE_W, (i + 1) * TUBE_W):
            for y in range(STRIP_H):
                strip.putpixel((x, y), (i * 40, 0, 0))
    tiles = strip_to_tiles(strip)
    assert len(tiles) == TUBE_COUNT
    for i, tile in enumerate(tiles):
        assert tile.size == (TUBE_W, TUBE_H)
        assert tile.getpixel((0, 0)) == (i * 40, 0, 0)
        assert tile.getpixel((TUBE_W - 1, TUBE_H - 1)) == (i * 40, 0, 0)


def test_strip_to_tube_bytes_lengths():
    strip = Image.new("RGB", (STRIP_W, STRIP_H), (255, 255, 255))
    payloads = strip_to_tube_bytes(strip)
    assert len(payloads) == TUBE_COUNT
    for p in payloads:
        assert len(p) == TUBE_W * TUBE_H * 2
        assert p == b"\xff\xff" * (TUBE_W * TUBE_H)
