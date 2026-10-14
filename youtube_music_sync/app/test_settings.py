import json
import os

import pytest

import settings


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("YMS_"):
            monkeypatch.delenv(key, raising=False)


def test_playlists_parsed_from_json(monkeypatch):
    monkeypatch.setenv("YMS_PLAYLISTS", json.dumps(["https://a", " https://b "]))
    assert settings.load().playlists == ("https://a", "https://b")


def test_playlists_fall_back_to_plain_lines(monkeypatch):
    # bashio handing us newline-separated values instead of JSON must not
    # leave the add-on thinking no playlists are configured.
    monkeypatch.setenv("YMS_PLAYLISTS", "https://a\nhttps://b")
    assert settings.load().playlists == ("https://a", "https://b")


@pytest.mark.parametrize("raw,expected", [("true", True), ("True", True), ("1", True),
                                          ("on", True), ("false", False), ("0", False),
                                          ("", True)])
def test_bool_options(monkeypatch, raw, expected):
    monkeypatch.setenv("YMS_DRY_RUN", raw)
    assert settings.load().dry_run is expected


def test_extra_args_are_shell_split(monkeypatch):
    monkeypatch.setenv("YMS_EXTRA_ARGS", '--sleep-requests 1 --user-agent "a b"')
    assert settings.load().extra_args == ("--sleep-requests", "1", "--user-agent", "a b")


def test_cookies_path_resolution(monkeypatch):
    monkeypatch.setenv("YMS_COOKIES_FILE", "cookies.txt")
    assert settings.load().resolved_cookies_file == "/config/cookies.txt"
    monkeypatch.setenv("YMS_COOKIES_FILE", "/share/c.txt")
    assert settings.load().resolved_cookies_file == "/share/c.txt"


def test_derived_paths():
    cfg = settings.Config(data_dir="/data", music_dir="/media/nas1/Music")
    assert cfg.db_path == "/data/state.db"
    assert cfg.tmp_dir == "/data/tmp"
    assert cfg.quarantine_dir == "/media/nas1/Music/.yt_duplicates"


def test_problems_require_playlists_and_sane_music_dir():
    assert any("No playlists" in p for p in settings.Config().problems())

    outside = settings.Config(playlists=("u",), music_dir="/tmp/music").problems()
    assert any("must be under /media or /share" in p for p in outside)

    root = settings.Config(playlists=("u",), music_dir="/media").problems()
    assert any("mount root itself" in p for p in root)

    ok = settings.Config(playlists=("u",), music_dir="/media/nas1/Music").problems()
    assert ok == []


def test_problems_flag_missing_cookies_file(tmp_path):
    cfg = settings.Config(playlists=("u",), music_dir="/media/nas1/Music",
                          cookies_file=str(tmp_path / "nope.txt"))
    assert any("cookies_file does not exist" in p for p in cfg.problems())
