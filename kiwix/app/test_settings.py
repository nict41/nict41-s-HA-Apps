from datetime import datetime

import settings


def test_unset_path_is_reported_not_guessed(monkeypatch):
    monkeypatch.setenv("ZIM_PATH", "")
    status = settings.storage_status()
    assert status["state"] == "unset"
    assert status["ok"] is False
    assert "ZIM storage path" in status["message"]


def test_relative_path_resolves_under_media_root(monkeypatch):
    monkeypatch.setenv("ZIM_PATH", "NAS1/Kiwix")
    assert settings.resolve_zim_path() == settings.MEDIA_ROOT / "NAS1" / "Kiwix"


def test_absolute_path_is_used_as_given(monkeypatch):
    monkeypatch.setenv("ZIM_PATH", "/media/NAS1/Kiwix")
    assert str(settings.resolve_zim_path()) == "/media/NAS1/Kiwix"


def test_unmounted_share_reports_the_mount_not_the_folder(monkeypatch):
    # Neither the share nor the folder exists: the useful message is about
    # the mount, since creating the folder would just write to HA's own disk.
    monkeypatch.setenv("ZIM_PATH", "GhostNAS/Kiwix")
    status = settings.storage_status()
    assert status["state"] == "unmounted"
    assert "isn't mounted" in status["message"]


def test_missing_folder_on_a_mounted_share_is_created(monkeypatch):
    share = settings.MEDIA_ROOT / "NAS2"
    share.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ZIM_PATH", "NAS2/Kiwix")

    status = settings.storage_status()

    assert status["ok"] is True
    assert (share / "Kiwix").is_dir()


def test_media_root_itself_is_refused(monkeypatch):
    monkeypatch.setenv("ZIM_PATH", "/media")
    status = settings.storage_status()
    assert status["state"] == "forbidden"
    assert status["ok"] is False


def test_read_only_share_is_reported(monkeypatch, zim_dir):
    monkeypatch.setattr(settings, "_writable", lambda path: False)
    status = settings.storage_status()
    assert status["state"] == "read_only"
    assert "not writable" in status["message"]


def test_healthy_share_reports_free_space(zim_dir):
    status = settings.storage_status()
    assert status["ok"] is True
    assert status["path"] == zim_dir
    assert status["free_bytes"] > 0


def test_options_are_exposed_for_the_ui():
    assert set(settings.as_dict()) == {
        "zim_path",
        "library_source",
        "catalog_language",
        "auto_serve_new",
        "max_concurrent_downloads",
        "download_threads",
        "window_enabled",
        "window_start",
        "window_end",
    }


# ── Runtime settings ────────────────────────────────────────────────────────

def test_settings_start_from_the_addon_options(monkeypatch):
    monkeypatch.setenv("DOWNLOAD_THREADS", "6")
    monkeypatch.setenv("MAX_CONCURRENT_DOWNLOADS", "2")
    settings.reset_cache()

    values = settings.all_settings()

    assert values["download_threads"] == 6
    assert values["max_concurrent_downloads"] == 2


def test_a_saved_setting_overrides_the_option_it_came_from(monkeypatch):
    monkeypatch.setenv("DOWNLOAD_THREADS", "2")

    settings.set_many({"download_threads": 8})

    assert settings.get("download_threads") == 8


def test_settings_are_clamped_and_junk_is_ignored():
    settings.set_many({"download_threads": 99, "max_concurrent_downloads": 0})
    assert settings.get("download_threads") == 8
    assert settings.get("max_concurrent_downloads") == 1

    settings.set_many({"download_threads": "not a number"})
    assert settings.get("download_threads") == 8

    settings.set_many({"unknown_key": 5})
    assert "unknown_key" not in settings.all_settings()


def test_settings_survive_a_restart():
    settings.set_many({"download_threads": 5, "window_start": "01:30"})
    settings.reset_cache()

    assert settings.get("download_threads") == 5
    assert settings.get("window_start") == "01:30"


def test_an_invalid_clock_time_is_refused_rather_than_stored():
    settings.set_many({"window_start": "23:00"})
    settings.set_many({"window_start": "25:00"})
    settings.set_many({"window_end": "midnight"})

    assert settings.get("window_start") == "23:00"


# ── Download window ─────────────────────────────────────────────────────────

def _at(hour, minute=0):
    return datetime(2026, 8, 30, hour, minute)


def test_the_window_is_open_when_it_is_switched_off():
    assert settings.window_open(_at(13)) is True
    assert settings.window_state(_at(13))["enabled"] is False


def test_a_window_crossing_midnight_covers_both_sides_of_it():
    settings.set_many({"window_enabled": True, "window_start": "23:00", "window_end": "07:00"})

    assert settings.window_open(_at(23, 30)) is True
    assert settings.window_open(_at(2)) is True
    assert settings.window_open(_at(6, 59)) is True
    assert settings.window_open(_at(7)) is False
    assert settings.window_open(_at(13)) is False


def test_a_same_day_window_needs_both_ends():
    settings.set_many({"window_enabled": True, "window_start": "09:00", "window_end": "17:00"})

    assert settings.window_open(_at(8, 59)) is False
    assert settings.window_open(_at(9)) is True
    assert settings.window_open(_at(16, 59)) is True
    assert settings.window_open(_at(17)) is False


def test_a_zero_length_window_is_treated_as_always_open():
    # Otherwise a slip of the finger would stall every download indefinitely.
    settings.set_many({"window_enabled": True, "window_start": "07:00", "window_end": "07:00"})

    assert settings.window_open(_at(3)) is True
    assert settings.window_open(_at(12)) is True
