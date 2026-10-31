import json
from datetime import date as real_date
from pathlib import Path

import pytest

import vision
import db


class FrozenDate(real_date):
    @classmethod
    def today(cls):
        return real_date(2026, 10, 31)


@pytest.fixture(autouse=True)
def freeze_today(monkeypatch):
    monkeypatch.setattr(vision, "date", FrozenDate)


CASES = json.loads((Path(__file__).parent / "reading_cases.json").read_text())


@pytest.mark.parametrize("case_id", sorted(CASES))
@pytest.mark.parametrize("reader", ["paddle", "glm"])
def test_saved_reader_cases_have_no_unrepaired_wrong_dates(case_id, reader):
    case = CASES[case_id]
    values = vision.date_candidates(case[reader])
    if case["truth"] is None:
        assert not values or all(c["repaired"] for c in values)
    else:
        assert all(c["date"] == case["truth"] or c["repaired"] for c in values)


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_saved_readers_never_confirm_wrong_dates(case_id):
    case = CASES[case_id]
    paddle = vision.date_candidates(case["paddle"])
    glm = vision.date_candidates(case["glm"])
    found, confidence, _ = db.reconcile_readings(paddle, glm)
    if case["truth"] is None:
        assert confidence != db.CONFIDENCE_CONFIRMED
    elif confidence == db.CONFIDENCE_CONFIRMED:
        assert found == case["truth"]


@pytest.mark.parametrize("text,expected", [
    ("GBC 211 204D", None), ("04:40 6941", None), ("BEST BEFORE", None),
    ("L8 20317", None), ("6 MARSHMALLOWS", None), ("10 mayonnaise", None),
    ("e 70g-2.5 OZ (6x11,7g)", None),
])
def test_non_dates(text, expected):
    assert vision.parse_transcript(text) == expected


def test_ranked_dates_and_repairs():
    assert vision.date_candidates("Use By:\n03 10V\nL8 20317")[0]["date"] == "2026-11-03"
    assert vision.date_candidates("Use By:\n03 10V\nL8 20317")[0]["repaired"] is True
    assert vision.date_candidates("L0B28 JN 2702:48")[0]["date"] == "2027-01-28"
    assert vision.date_candidates("L0B28 JN 2702:48")[0]["repaired"] is True
    assert vision.parse_transcript("Prod 09.03.2026\nBB 30.09.2027") == "2027-09-30"
    assert vision.parse_transcript("BB 01/05/2027\nPacked 01/06/2027") == "2027-05-01"
    ranked = vision.date_candidates("L8 190327\nBB 30/09/2027")
    assert ranked[0]["date"] == "2027-09-30"


def test_reconcile_candidate_rules():
    def c(day, repaired=False): return {"date": f"2027-01-{day:02d}", "repaired": repaired}
    assert db.reconcile_readings([c(1)], [c(1)]) == ("2027-01-01", "confirmed", [])
    assert db.reconcile_readings([c(1), c(2)], [c(3), c(2)]) == ("2027-01-02", "confirmed", [])
    assert db.reconcile_readings([c(1)], [c(2)]) == (None, "conflict", ["2027-01-01", "2027-01-02"])
    assert db.reconcile_readings([c(1, True)], [c(1, True)]) == ("2027-01-01", "check", [])
    assert db.reconcile_readings([c(1)], []) == ("2027-01-01", "check", [])
    assert db.reconcile_readings([], []) == (None, "unread", [])


def test_a_printed_year_is_not_also_read_as_a_yearless_date():
    assert [c["date"] for c in vision.date_candidates("28 SEP 26")] == ["2026-09-28"]


def test_month_repairs_prefer_the_longest_match():
    assert vision.date_candidates("03 110V")[0]["date"] == "2026-11-03"
    assert vision.date_candidates("03 14OV")[0]["date"] == "2026-11-03"


def test_a_future_date_beats_a_past_one_under_the_same_label():
    assert vision.date_candidates("BEST BEFORE\n01.10.2026\n30.09.2027")[0]["date"] == "2027-09-30"


def test_a_repaired_date_is_never_confirmed():
    def c(day, repaired=False): return {"date": f"2027-01-{day:02d}", "repaired": repaired}
    assert db.reconcile_readings([c(1, True)], [c(1)]) == ("2027-01-01", "check", [])
    assert db.reconcile_readings([c(1), c(2, True)], [c(3), c(2)]) == ("2027-01-02", "check", [])


def test_a_batch_code_shared_by_nothing_loses_to_the_shared_date():
    bb = {"date": "2027-09-30", "repaired": False}
    batch = {"date": "2027-03-19", "repaired": False}
    assert db.reconcile_readings([batch, bb], [bb]) == ("2027-09-30", "confirmed", [])


@pytest.mark.parametrize(("text", "expected"), [
    ("Best Before APR 2027", "2027-04-30"),
    ("Best Before April 2027", "2027-04-30"),
    ("Best Before 04/2027", "2027-04-30"),
    ("Best Before 04.2027", "2027-04-30"),
    ("Best Before 04 2027", "2027-04-30"),
    ("BBE APR 27", "2027-04-30"),
    ("Best Before End: 04/27", "2027-04-30"),
    ("BestBefore End:\nFILM\nAPR 2027", "2027-04-30"),
])
def test_labelled_month_year_dates(text, expected):
    assert vision.parse_transcript(text) == expected


@pytest.mark.parametrize("text", [
    "28 JAN 27", "03/11", "APR 27", "6239 11:04 BLA", "FILM", "RECYCLE AT",
])
def test_month_year_additions_do_not_reinterpret_non_month_year_text(text):
    if text == "28 JAN 27":
        assert vision.parse_transcript(text) == "2027-01-28"
    elif text == "03/11":
        assert vision.parse_transcript(text) == "2026-11-03"
    else:
        assert vision.parse_transcript(text) is None


def test_full_date_outranks_month_year_on_same_transcript():
    assert vision.date_candidates("BEST BEFORE\nAPR 2027\n30/04/2027")[0]["date"] == "2027-04-30"


def test_case_18_month_year_is_read_by_paddle():
    case = CASES["18"]
    assert vision.date_candidates(case["paddle"])[0]["date"] == case["truth"]


def test_month_name_must_not_match_inside_a_word():
    assert vision.parse_transcript("Best Before APRE 2027") is None


@pytest.mark.parametrize("text", ["Best Before APR 2026", "Best Before APR 2037"])
def test_month_year_uses_the_normal_past_and_future_windows(text):
    assert vision.parse_transcript(text) is None


@pytest.mark.parametrize("text,expected", [
    ("APR 2027", "2027-04-30"),
    ("04/2027", "2027-04-30"),
    ("BestBefore End:\nAPR 2027\nL8 190327", "2027-04-30"),
    ("Dobbs APR 27", None),
    ("F5T6 BBXY 04/27", None),
    ("BB 04/27", "2027-04-30"),
])
def test_month_year_needs_a_full_year_or_a_real_label(text, expected):
    ranked = vision.date_candidates(text)
    assert (ranked[0]["date"] if ranked else None) == expected


@pytest.mark.parametrize("text,confident", [
    ("BB 28/09/2027", True),
    ("28/09/2027", True),
    ("190327", False),
    ("APR 2027", False),
    ("03 10V", False),
])
def test_live_capture_needs_a_confident_read(text, confident):
    assert vision.is_confident(vision.date_candidates(text)[0]) is confident
