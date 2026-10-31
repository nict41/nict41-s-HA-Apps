import json
from io import BytesIO

from PIL import Image
import pytest

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
    _freeze_today(monkeypatch, 2026, 10, 30)
    _mock_reply(monkeypatch, "2026-12-01")
    result = vision.extract_use_by_date(_jpeg_bytes())
    assert result == {"date": "2026-12-01", "raw": "2026-12-01"}


def test_extracts_day_month_year_slash_format(monkeypatch):
    monkeypatch.setenv("VISION_API_BASE", "http://model.local/api")
    _freeze_today(monkeypatch, 2026, 10, 30)
    _mock_reply(monkeypatch, "01/12/2026")
    result = vision.extract_use_by_date(_jpeg_bytes())
    assert result["date"] == "2026-12-01"


def test_extracts_text_month_with_two_digit_year(monkeypatch):
    monkeypatch.setenv("VISION_API_BASE", "http://model.local/api")
    _freeze_today(monkeypatch, 2026, 10, 30)
    _mock_reply(monkeypatch, "1 Dec 26")
    result = vision.extract_use_by_date(_jpeg_bytes())
    assert result["date"] == "2026-12-01"


def _date_read_from(monkeypatch, reply: str) -> str | None:
    monkeypatch.setenv("VISION_API_BASE", "http://model.local/api")
    _freeze_today(monkeypatch, 2026, 10, 30)
    _mock_reply(monkeypatch, reply)
    return vision.extract_use_by_date(_jpeg_bytes())["date"]


def test_missing_year_resolves_to_this_years_occurrence_when_still_upcoming(monkeypatch):
    assert _date_read_from(monkeypatch, "31 Oct") == "2026-10-31"


def test_missing_year_rolls_to_next_year_once_this_years_date_is_long_gone(monkeypatch):
    assert _date_read_from(monkeypatch, "15 Jan") == "2027-01-15"


def test_missing_year_keeps_a_recent_past_date_so_it_shows_as_expired(monkeypatch):
    assert _date_read_from(monkeypatch, "20 Oct") == "2026-10-20"


def test_time_after_a_date_is_not_mistaken_for_a_year(monkeypatch):
    assert _date_read_from(monkeypatch, "02 Nov 11:26") == "2026-11-02"


def test_date_run_together_with_codes_and_a_time(monkeypatch):
    assert _date_read_from(monkeypatch, "L0828JAN2702:48") == "2027-01-28"


def test_numeric_day_month_with_no_year(monkeypatch):
    assert _date_read_from(monkeypatch, "03/11") == "2026-11-03"


def test_invented_year_years_in_the_past_is_replaced(monkeypatch):
    assert _date_read_from(monkeypatch, "2021-11-02") == "2026-11-02"
    assert _date_read_from(monkeypatch, "14 MAR 2023") == "2027-03-14"


def test_printed_year_a_few_weeks_past_is_left_alone(monkeypatch):
    assert _date_read_from(monkeypatch, "01/09/2026") == "2026-09-01"


def test_garbled_date_gives_no_date_rather_than_a_guess(monkeypatch):
    assert _date_read_from(monkeypatch, "03110V") is None


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


@pytest.mark.parametrize(
    ("transcript", "expected"),
    [
        ("01 03 27", "2027-03-01"),
        ("010327", "2027-03-01"),
        ("0103 27", "2027-03-01"),
        ("26 12 26", "2026-12-26"),
        ("261226", "2026-12-26"),
        ("BB:28/09/2027\n6271 EV 05:09", "2027-09-28"),
        ("BEST BEFORE\n26 12 26\nGBC 211 204D\n04:40 6941", "2026-12-26"),
        ("Use By:\n02 Nov 14:26 F5 T6 BBX Y", "2026-11-02"),
        ("L 08 28 JAN 27 02:48", "2027-01-28"),
        ("L 08 28 JAN 27 02:48\nDATE: 27", "2027-01-28"),
        ("09:03:2026\n50.09.2027:12\ne 70g-2.5 OZ (6x11,7g)", None),
        ("04:40 6941", None),
        ("GBC 211 204D", None),
        ("L8 20317\nKeep refrigerated", None),
        ("Use By:\n03 10V\nL8 20317", None),
        ("BEST BEFORE", None),
        ("08.03.2026\n30.03.2025:12", None),
        ("BB 28/09/2027\n30/09/2027", "2027-09-30"),
        ("DATE: UNKNOWN", None),
        ("6 MARSHMALLOWS", None),
        ("10 mayonnaise", None),
    ],
)
def test_parse_transcript(monkeypatch, transcript, expected):
    _freeze_today(monkeypatch, 2026, 10, 31)
    assert vision.parse_transcript(transcript) == expected


def test_prompt_is_selected_for_glm_ocr(monkeypatch):
    monkeypatch.setenv("VISION_MODEL", "glm-ocr:latest")
    assert vision._prompt_for(vision.settings.vision_model()) == "Text Recognition:"


def test_prompt_for_other_models_requests_printed_lines():
    assert "write out every line" in vision._prompt_for("qwen2.5vl:3b")
