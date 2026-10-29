import json
from io import BytesIO

from PIL import Image

import vision


def _jpeg_bytes() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (20, 20), "white").save(buf, format="JPEG")
    return buf.getvalue()


class _FakeResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _mock_reply(monkeypatch, content: str):
    payload = {"choices": [{"message": {"content": content}}]}
    monkeypatch.setattr(vision.urllib.request, "urlopen", lambda req, timeout: _FakeResponse(payload))


def test_not_configured_skips_network_entirely(monkeypatch):
    monkeypatch.setenv("VISION_API_BASE", "")
    result = vision.extract_use_by_date(_jpeg_bytes())
    assert result == {"date": None, "raw": "Vision model is not configured"}


def test_extracts_iso_date(monkeypatch):
    monkeypatch.setenv("VISION_API_BASE", "http://model.local/api")
    _mock_reply(monkeypatch, "2026-12-01")
    result = vision.extract_use_by_date(_jpeg_bytes())
    assert result == {"date": "2026-12-01", "raw": "2026-12-01"}


def test_extracts_day_month_year_slash_format(monkeypatch):
    monkeypatch.setenv("VISION_API_BASE", "http://model.local/api")
    _mock_reply(monkeypatch, "01/12/2026")
    result = vision.extract_use_by_date(_jpeg_bytes())
    assert result["date"] == "2026-12-01"


def test_extracts_text_month_with_two_digit_year(monkeypatch):
    monkeypatch.setenv("VISION_API_BASE", "http://model.local/api")
    _mock_reply(monkeypatch, "1 Dec 26")
    result = vision.extract_use_by_date(_jpeg_bytes())
    assert result["date"] == "2026-12-01"


def test_unknown_reply_returns_no_date(monkeypatch):
    monkeypatch.setenv("VISION_API_BASE", "http://model.local/api")
    _mock_reply(monkeypatch, "UNKNOWN")
    result = vision.extract_use_by_date(_jpeg_bytes())
    assert result == {"date": None, "raw": "UNKNOWN"}


def test_unparseable_reply_returns_no_date_but_keeps_raw_text(monkeypatch):
    monkeypatch.setenv("VISION_API_BASE", "http://model.local/api")
    _mock_reply(monkeypatch, "I can't tell, sorry")
    result = vision.extract_use_by_date(_jpeg_bytes())
    assert result["date"] is None
    assert "sorry" in result["raw"]


def test_network_error_degrades_to_no_date(monkeypatch):
    monkeypatch.setenv("VISION_API_BASE", "http://model.local/api")

    def _raise(req, timeout):
        raise TimeoutError("slow model")

    monkeypatch.setattr(vision.urllib.request, "urlopen", _raise)
    result = vision.extract_use_by_date(_jpeg_bytes())
    assert result["date"] is None
    assert "failed" in result["raw"]
