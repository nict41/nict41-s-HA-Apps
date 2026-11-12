"""night.py: pure daylight/brightness/dim/quiet maths and parse_night - no Pillow."""

import datetime as dt

import pytest

from night import (MAX_BRIGHTNESS, MIN_BRIGHTNESS, NightConfig, QuietHours,
                   brightness, daylight, dim_factor, in_quiet, parse_night)


# ----------------------------------------------------------------------
# daylight
# ----------------------------------------------------------------------

def test_daylight_full_day_and_full_night():
    assert daylight(10.0, 3.0, -6.0) == 1.0
    assert daylight(3.0, 3.0, -6.0) == 1.0      # at bright_above -> full day
    assert daylight(-6.0, 3.0, -6.0) == 0.0     # at dim_below -> full night
    assert daylight(-20.0, 3.0, -6.0) == 0.0


def test_daylight_linear_in_between():
    # halfway between -6 and 3 is -1.5
    assert daylight(-1.5, 3.0, -6.0) == pytest.approx(0.5)
    assert daylight(0.0, 3.0, -6.0) == pytest.approx(6 / 9)


def test_daylight_degenerate_range_is_a_hard_step():
    assert daylight(5.0, 3.0, 3.0) == 1.0
    assert daylight(2.9, 3.0, 3.0) == 0.0
    assert daylight(3.0, -6.0, -6.0) == 1.0    # bright_above <= dim_below
    assert daylight(-7.0, -6.0, -6.0) == 0.0


# ----------------------------------------------------------------------
# brightness
# ----------------------------------------------------------------------

def test_brightness_endpoints_and_clamp():
    assert brightness(1.0, 255, 50) == 255
    assert brightness(0.0, 255, 50) == 50
    assert brightness(0.5, 255, 50) == 152 and isinstance(brightness(0.5, 255, 50), int)
    # clamped to 10..255 even for silly inputs
    assert brightness(1.0, 999, 50) == MAX_BRIGHTNESS
    assert brightness(0.0, 255, 1) == MIN_BRIGHTNESS
    assert brightness(2.0, 255, 50) == MAX_BRIGHTNESS      # daylight > 1 clamped
    assert brightness(-1.0, 255, 50) == 50                 # daylight < 0 clamped to night


# ----------------------------------------------------------------------
# dim_factor
# ----------------------------------------------------------------------

def test_dim_factor():
    assert dim_factor(1.0, 0.6) == pytest.approx(1.0)       # full day: never dim
    assert dim_factor(0.0, 0.6) == pytest.approx(0.6)       # full night: dim_screens
    assert dim_factor(0.5, 0.6) == pytest.approx(0.8)
    assert dim_factor(0.0, 1.0) == pytest.approx(1.0)       # 1 = never dim
    assert dim_factor(0.0, 0.0) == pytest.approx(0.0)


# ----------------------------------------------------------------------
# in_quiet
# ----------------------------------------------------------------------

def test_in_quiet_simple_window():
    start, end = dt.time(23, 0), dt.time(7, 0)
    assert in_quiet(dt.time(23, 0), start, end)
    assert in_quiet(dt.time(0, 0), start, end)
    assert in_quiet(dt.time(6, 59), start, end)
    assert not in_quiet(dt.time(7, 0), start, end)      # end is exclusive
    assert not in_quiet(dt.time(12, 0), start, end)
    assert not in_quiet(dt.time(22, 59), start, end)


def test_in_quiet_same_day_window():
    start, end = dt.time(12, 0), dt.time(14, 0)
    assert in_quiet(dt.time(12, 0), start, end)
    assert in_quiet(dt.time(13, 30), start, end)
    assert not in_quiet(dt.time(14, 0), start, end)
    assert not in_quiet(dt.time(11, 59), start, end)


def test_in_quiet_start_equals_end_means_never():
    t = dt.time(8, 0)
    assert not in_quiet(t, t, t)
    assert not in_quiet(dt.time(0, 0), t, t)


# ----------------------------------------------------------------------
# parse_night
# ----------------------------------------------------------------------

def test_parse_night_absent():
    cfg, errors = parse_night(None)
    assert cfg is None and errors == []


def test_parse_night_defaults():
    cfg, errors = parse_night({})
    assert errors == []
    assert cfg == NightConfig()
    assert cfg.brightness_day == 255 and cfg.brightness_night == 10
    assert cfg.bright_above == 3.0 and cfg.dim_below == -6.0
    assert cfg.dim_screens == 1.0 and cfg.quiet is None


def test_parse_night_full_block():
    cfg, errors = parse_night({
        "brightness_day": 200, "brightness_night": 30,
        "bright_above": 5, "dim_below": -3, "dim_screens": 0.5,
        "quiet": {"from": "23:30", "to": "06:15", "mode": "blank"},
    })
    assert errors == []
    assert cfg.brightness_day == 200 and cfg.brightness_night == 30
    assert cfg.bright_above == 5.0 and cfg.dim_below == -3.0
    assert cfg.dim_screens == 0.5
    assert cfg.quiet == QuietHours(dt.time(23, 30), dt.time(6, 15), "blank")


def test_parse_night_quiet_default_mode_is_clock():
    cfg, errors = parse_night({"quiet": {"from": "22:00", "to": "07:00"}})
    assert errors == []
    assert cfg.quiet.mode == "clock"


def test_parse_night_errors_return_none_and_messages():
    cfg, errors = parse_night({"brightness_day": 5})       # below 10
    assert cfg is None and any("brightness_day" in e for e in errors)
    cfg, errors = parse_night({"dim_screens": 2})           # above 1
    assert cfg is None and any("dim_screens" in e for e in errors)
    cfg, errors = parse_night({"bright_above": 100})        # above 90
    assert cfg is None and any("bright_above" in e for e in errors)
    cfg, errors = parse_night({"quiet": {"from": "25:00", "to": "07:00"}})
    assert cfg is None and any("quiet.from" in e for e in errors)
    cfg, errors = parse_night({"quiet": {"from": "23:00", "to": "07:00", "mode": "sleep"}})
    assert cfg is None and any("mode" in e for e in errors)
    cfg, errors = parse_night("not a mapping")
    assert cfg is None and errors


def test_parse_night_partial_block_keeps_defaults():
    cfg, errors = parse_night({"brightness_night": 40})
    assert errors == []
    assert cfg.brightness_night == 40
    assert cfg.brightness_day == 255      # untouched default
