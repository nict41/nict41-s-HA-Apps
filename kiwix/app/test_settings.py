import os

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
    }
