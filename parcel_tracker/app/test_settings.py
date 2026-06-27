import pytest

import db
import settings


@pytest.fixture(autouse=True)
def _fresh_db():
    db.DB_PATH.unlink(missing_ok=True)
    db.init_db()
    yield


def test_unset_settings_fall_back_to_defaults():
    values = settings.all_settings()
    assert values["poll_interval_minutes"] == settings._DEFAULTS["poll_interval_minutes"]
    assert values["trusted_senders"] == settings._DEFAULTS["trusted_senders"]


def test_set_and_get_override():
    settings.set_many({"poll_interval_minutes": 45})
    assert settings.get_int("poll_interval_minutes") == 45


def test_integer_settings_are_clamped_into_range():
    settings.set_many({"poll_interval_minutes": 99999})
    assert settings.get_int("poll_interval_minutes") == 1440  # upper bound
    settings.set_many({"poll_interval_minutes": 1})
    assert settings.get_int("poll_interval_minutes") == 5  # lower bound


def test_invalid_integer_is_ignored_on_write_and_falls_back_on_read():
    settings.set_many({"lookback_days": "not a number"})
    assert settings.get_int("lookback_days") == settings._DEFAULTS["lookback_days"]


def test_get_domains_normalises_and_splits():
    settings.set_many({"ignore_senders": " A.com,  b.COM , ,c.com"})
    assert settings.get_domains("ignore_senders") == frozenset({"a.com", "b.com", "c.com"})


def test_set_many_ignores_unknown_keys():
    settings.set_many({"not_a_real_setting": "x"})
    assert "not_a_real_setting" not in settings.all_settings()


def test_set_many_returns_full_settings_dict():
    result = settings.set_many({"lookback_days": 7})
    assert result["lookback_days"] == 7
    assert set(result) == set(settings.INT_KEYS) | set(settings.STR_KEYS)
