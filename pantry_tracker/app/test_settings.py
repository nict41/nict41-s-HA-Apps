import settings


def test_get_int_falls_back_to_default_when_nothing_stored():
    # _DEFAULTS is captured once at import time (from the run script's env),
    # not re-read live - get_int only falls back to it when no override has
    # ever been saved, which is what's being exercised here.
    assert settings.get_int("notify_days_before") == settings._DEFAULTS["notify_days_before"]
    assert settings.get_str("notify_service") == settings._DEFAULTS["notify_service"]


def test_set_many_persists_and_clamps():
    result = settings.set_many(
        {
            "notify_days_before": 999,
            "notify_repeat_hours": 0,
            "check_interval_hours": 6,
            "notify_service": "  notify.mobile_app_phone  ",
        }
    )
    assert result["notify_days_before"] == 30  # clamped to max
    assert result["notify_repeat_hours"] == 1  # clamped to min
    assert result["check_interval_hours"] == 6
    assert result["notify_service"] == "notify.mobile_app_phone"


def test_set_many_ignores_invalid_numbers():
    before = settings.get_int("check_interval_hours")
    settings.set_many({"check_interval_hours": "not-a-number"})
    assert settings.get_int("check_interval_hours") == before


def test_set_many_ignores_unknown_keys():
    result = settings.set_many({"unknown_key": "value"})
    assert "unknown_key" not in result


def test_all_settings_returns_every_key():
    values = settings.all_settings()
    for key in settings.INT_KEYS + settings.STR_KEYS:
        assert key in values


def test_vision_helpers_read_environment(monkeypatch):
    monkeypatch.setenv("VISION_API_BASE", "http://model.local/api/")
    monkeypatch.setenv("VISION_API_KEY", "secret")
    monkeypatch.setenv("VISION_MODEL", "")
    assert settings.vision_api_base() == "http://model.local/api"
    assert settings.vision_api_key() == "secret"
    assert settings.vision_model() == "qwen2.5vl:3b"  # falls back when blank
