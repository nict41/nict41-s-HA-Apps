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


def _freeze_today(monkeypatch, year: int, month: int, day: int):
    from datetime import date as _date

    class _FixedDate(_date):
        @classmethod
        def today(cls):
            return _date(year, month, day)

    monkeypatch.setattr(vision, "date", _FixedDate)


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


def test_missing_year_resolves_to_this_years_occurrence_when_still_upcoming(monkeypatch):
    monkeypatch.setenv("VISION_API_BASE", "http://model.local/api")
    _freeze_today(monkeypatch, 2026, 10, 30)
    _mock_reply(monkeypatch, "0000-10-31")
    result = vision.extract_use_by_date(_jpeg_bytes())
    assert result["date"] == "2026-10-31"


def test_missing_year_rolls_to_next_year_once_this_years_date_has_passed(monkeypatch):
    monkeypatch.setenv("VISION_API_BASE", "http://model.local/api")
    _freeze_today(monkeypatch, 2026, 10, 30)
    _mock_reply(monkeypatch, "0000-01-15")
    result = vision.extract_use_by_date(_jpeg_bytes())
    assert result["date"] == "2027-01-15"


def test_extracts_day_month_with_no_year_token_at_all(monkeypatch):
    monkeypatch.setenv("VISION_API_BASE", "http://model.local/api")
    _freeze_today(monkeypatch, 2026, 10, 30)
    _mock_reply(monkeypatch, "31 Oct")
    result = vision.extract_use_by_date(_jpeg_bytes())
    assert result["date"] == "2026-10-31"


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
