import settings


def test_get_int_falls_back_to_default_when_nothing_stored():
    # _DEFAULTS is captured once at import time (from the run script's env),
    # not re-read live - get_int only falls back to it when no override has
    # ever been saved, which is what's being exercised here.
    assert settings.get_int("notify_days_before") == settings._DEFAULTS["notify_days_before"]


def test_set_many_persists_and_clamps():
    result = settings.set_many(
        {
            "notify_days_before": 999,
            "notify_repeat_hours": 0,
            "check_interval_hours": 6,
        }
    )
    assert result["notify_days_before"] == 30  # clamped to max
    assert result["notify_repeat_hours"] == 1  # clamped to min
    assert result["check_interval_hours"] == 6


def test_set_many_ignores_invalid_numbers():
    before = settings.get_int("check_interval_hours")
    settings.set_many({"check_interval_hours": "not-a-number"})
    assert settings.get_int("check_interval_hours") == before


def test_set_many_ignores_unknown_keys():
    result = settings.set_many({"unknown_key": "value"})
    assert "unknown_key" not in result


def test_set_many_does_not_accept_notify_service():
    # notify_service is add-on-configuration-only now - see the module
    # docstring for why (a DB-saved override, even blank, used to be able
    # to permanently shadow the Configuration tab).
    settings.set_many({"notify_service": "notify.mobile_app_phone"})
    assert settings.all_settings()["notify_service"] == settings.notify_service()


def test_notify_service_reads_environment_live(monkeypatch):
    monkeypatch.setenv("NOTIFY_SERVICE", "notify.mobile_app_phone")
    assert settings.notify_service() == "notify.mobile_app_phone"
    monkeypatch.setenv("NOTIFY_SERVICE", "")
    assert settings.notify_service() == ""


def test_all_settings_returns_every_key():
    values = settings.all_settings()
    for key in settings.INT_KEYS:
        assert key in values
    assert "notify_service" in values


def test_vision_helpers_read_environment(monkeypatch):
    monkeypatch.setenv("VISION_API_BASE", "http://model.local/api/")
    monkeypatch.setenv("VISION_API_KEY", "secret")
    monkeypatch.setenv("VISION_MODEL", "")
    assert settings.vision_api_base() == "http://model.local/api"
    assert settings.vision_api_key() == "secret"
    assert settings.vision_model() == "qwen2.5vl:3b"  # falls back when blank
