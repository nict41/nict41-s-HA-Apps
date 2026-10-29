from io import BytesIO
from types import SimpleNamespace

from PIL import Image

import barcode


def _blank_jpeg_bytes() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (40, 40), "white").save(buf, format="JPEG")
    return buf.getvalue()


def test_decode_barcode_returns_none_for_unreadable_bytes():
    assert barcode.decode_barcode(b"not an image") is None


def test_decode_barcode_returns_none_when_zbar_finds_nothing(monkeypatch):
    monkeypatch.setattr(barcode, "_zbar_decode", lambda image: [])
    assert barcode.decode_barcode(_blank_jpeg_bytes()) is None


def test_decode_barcode_returns_first_product_symbol(monkeypatch):
    fake_symbols = [
        SimpleNamespace(type="QRCODE", data=b"https://example.com"),
        SimpleNamespace(type="EAN13", data=b"5000112637922"),
    ]
    monkeypatch.setattr(barcode, "_zbar_decode", lambda image: fake_symbols)
    assert barcode.decode_barcode(_blank_jpeg_bytes()) == "5000112637922"


def test_decode_barcode_ignores_non_product_symbols(monkeypatch):
    fake_symbols = [SimpleNamespace(type="QRCODE", data=b"https://example.com")]
    monkeypatch.setattr(barcode, "_zbar_decode", lambda image: fake_symbols)
    assert barcode.decode_barcode(_blank_jpeg_bytes()) is None
