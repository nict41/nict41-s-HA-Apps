"""Options handling, and the safety constants that protect the printer."""

import pytest

import settings


# ── camera_entity ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", [
    "camera.centauri_carbon_2_chamber_camera",
    "camera.x",
    "camera.a_b_9",
])
def test_valid_entity_ids_are_kept(monkeypatch, value):
    monkeypatch.setenv("CAMERA_ENTITY", value)
    assert settings.camera_entity() == value


@pytest.mark.parametrize("value", [
    "",
    "sensor.not_a_camera",
    "camera.Upper_Case",
    "camera.has-dashes",
    "camera.trailing/../../auth/token",
    "camera.with space",
    "camera.",
])
def test_malformed_entity_ids_fall_back_to_the_default(monkeypatch, value):
    """The entity id is interpolated into a Home Assistant API path, so
    anything that isn't `camera.<lowercase_id>` must never reach a URL."""
    monkeypatch.setenv("CAMERA_ENTITY", value)
    assert settings.camera_entity() == settings.CAMERA_ENTITY_DEFAULT


# ── Numbers ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("1", 1), ("5", 5), ("15", 15),
    ("0", 1), ("99", 15),          # clamped to config.yaml's int(1,15)
    ("", 5), ("banana", 5),        # unset or nonsense -> the default
])
def test_fps_is_clamped_to_the_schema(monkeypatch, raw, expected):
    monkeypatch.setenv("FPS", raw)
    assert settings.fps() == expected


@pytest.mark.parametrize("raw,expected", [
    ("1", 1), ("20", 20), ("0", 1), ("1000", 20), ("", 5),
])
def test_max_viewers_is_clamped_to_the_schema(monkeypatch, raw, expected):
    monkeypatch.setenv("MAX_VIEWERS", raw)
    assert settings.max_viewers() == expected


def test_blank_page_title_falls_back(monkeypatch):
    monkeypatch.setenv("PAGE_TITLE", "   ")
    assert settings.page_title() == settings.PAGE_TITLE_DEFAULT


# ── Home Assistant endpoint ─────────────────────────────────────────────────

def test_ha_api_defaults_to_the_supervisor_proxy(monkeypatch):
    monkeypatch.delenv("HA_API", raising=False)
    assert settings.ha_api() == "http://supervisor/core/api"


def test_ha_api_override_is_normalised(monkeypatch):
    monkeypatch.setenv("HA_API", "http://example.test/core/api/")
    assert settings.ha_api() == "http://example.test/core/api"


def test_ha_url_is_ignored(monkeypatch):
    """HA_URL collides with other tooling on this stack, so it is deliberately
    not a name this app reads."""
    monkeypatch.delenv("HA_API", raising=False)
    monkeypatch.setenv("HA_URL", "http://somewhere-else.test")
    assert settings.ha_api() == "http://supervisor/core/api"


# ── Access key ──────────────────────────────────────────────────────────────

def test_no_key_means_no_auth(monkeypatch):
    monkeypatch.setenv("ACCESS_KEY", "")
    assert settings.auth_required() is False
    assert settings.key_matches(None) is True
    assert settings.cookie_matches(None) is True


def test_key_matching(monkeypatch):
    monkeypatch.setenv("ACCESS_KEY", "s3cret")
    assert settings.auth_required() is True
    assert settings.key_matches("s3cret") is True
    assert settings.key_matches("S3CRET") is False
    assert settings.key_matches("s3cre") is False
    assert settings.key_matches("s3secret-and-more") is False
    assert settings.key_matches("") is False
    assert settings.key_matches(None) is False


def test_cookie_holds_a_digest_not_the_key(monkeypatch):
    monkeypatch.setenv("ACCESS_KEY", "s3cret")
    value = settings.cookie_value()
    assert "s3cret" not in value
    assert len(value) == 64
    assert settings.cookie_matches(value) is True
    assert settings.cookie_matches("s3cret") is False
    assert settings.cookie_matches(settings.key_digest("other")) is False


def test_options_summary_never_includes_the_key(monkeypatch):
    monkeypatch.setenv("ACCESS_KEY", "s3cret")
    summary = settings.as_dict()
    assert summary["access_key_set"] is True
    assert "s3cret" not in repr(summary)


# ── The constants that keep the printer alive ───────────────────────────────

def test_backoff_and_stall_values_are_sane():
    """These are not options: they are properties of a camera that allows one
    connection and crashes when something opens a second."""
    assert settings.BACKOFF_START_SECONDS >= 1.0
    assert settings.BACKOFF_MAX_SECONDS >= settings.BACKOFF_START_SECONDS * 8
    assert settings.BACKOFF_MAX_SECONDS <= 120.0
    assert settings.BACKOFF_JITTER > 0
    assert settings.STALL_SECONDS >= 5.0
    assert settings.IDLE_SHUTDOWN_SECONDS > 0
    # Shorter than the integration's own 60 s hub shutdown, so this app is
    # never the thing holding the printer's camera open.
    assert settings.IDLE_SHUTDOWN_SECONDS < 60.0
