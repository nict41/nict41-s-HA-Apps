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


# ------------------------------------------------------- playlist de-duplication

def test_duplicate_playlist_urls_differing_only_by_si_are_collapsed(monkeypatch):
    monkeypatch.setenv("YMS_PLAYLISTS", json.dumps([
        "https://music.youtube.com/playlist?list=PLvsKMOn5as94GH_4-P2Z0l2eRJY_6JidE&si=p-TGNQhEMw2NIjVn",
        "https://music.youtube.com/playlist?list=PLvsKMOn5as94GH_4-P2Z0l2eRJY_6JidE&si=O2ElVt-FJQM5z94i",
    ]))
    playlists = settings.load().playlists
    assert len(playlists) == 1
    assert playlists[0].endswith("si=p-TGNQhEMw2NIjVn")


def test_duplicate_playlist_urls_are_the_same_across_youtube_hosts(monkeypatch):
    monkeypatch.setenv("YMS_PLAYLISTS", json.dumps([
        "https://www.youtube.com/playlist?list=PLabc",
        "https://youtube.com/playlist?list=PLabc",
        "https://music.youtube.com/playlist?list=PLabc",
    ]))
    assert len(settings.load().playlists) == 1


def test_different_playlists_are_both_kept(monkeypatch):
    monkeypatch.setenv("YMS_PLAYLISTS", json.dumps([
        "https://music.youtube.com/playlist?list=PLaaa",
        "https://music.youtube.com/playlist?list=PLbbb",
    ]))
    assert len(settings.load().playlists) == 2


def test_a_url_with_no_list_param_is_never_treated_as_a_duplicate(monkeypatch):
    # Doesn't look like a playlist at all by this check - left alone rather
    # than risking dropping something that should have been kept.
    monkeypatch.setenv("YMS_PLAYLISTS", json.dumps([
        "https://music.youtube.com/watch?v=abc",
        "https://music.youtube.com/watch?v=abc",
    ]))
    assert len(settings.load().playlists) == 2


def test_a_non_youtube_url_is_left_alone(monkeypatch):
    monkeypatch.setenv("YMS_PLAYLISTS", json.dumps([
        "https://example.com/playlist?list=PLabc",
        "https://example.com/playlist?list=PLabc",
    ]))
    assert len(settings.load().playlists) == 2


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


# ------------------------------------------------------- add-on schema shape

def _config_yaml_sections():
    """Parse config.yaml's options/schema blocks without needing PyYAML.

    The test suite's only dependency is pytest, and this needs to run in CI,
    so the two flat blocks are read by hand rather than pulling in a parser.
    """
    import os
    import re

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.yaml")
    with open(path, "r", encoding="utf-8") as fh:
        lines = fh.read().splitlines()

    sections: dict[str, dict[str, str]] = {"options": {}, "schema": {}}
    current = None
    for line in lines:
        if line.rstrip() in ("options:", "schema:"):
            current = line.rstrip().rstrip(":")
            continue
        if current is None:
            continue
        if line and not line.startswith(" "):
            current = None
            continue
        match = re.match(r"^  ([a-z0-9_]+):\s*(.*?)\s*$", line)
        if match:
            sections[current][match.group(1)] = match.group(2)
    return sections["options"], sections["schema"]


def test_every_option_has_a_schema_entry_and_the_reverse():
    options, schema = _config_yaml_sections()
    assert options and schema
    assert set(options) == set(schema)


def test_every_scalar_option_is_optional():
    """Home Assistant refuses to save when a required option has no value.

    Updating an add-on does not backfill new defaults into an install's saved
    options, so an option added in a later version is simply absent on every
    existing install. Declared required, that makes *every* save fail with
    "Missing option '<name>' in root" - including the save that would have
    supplied it. Both the startup script and settings.py already substitute a
    default for an absent option, so nothing here needs to be mandatory.
    """
    _options, schema = _config_yaml_sections()
    required = {
        name: rule
        for name, rule in schema.items()
        # A list option ("playlists") has its type on the following line and
        # is not a scalar rule; it is not subject to this.
        if rule and not rule.strip('"').endswith("?")
    }
    assert required == {}, (
        "these options are required and will break saving for anyone whose "
        f"install predates them: {sorted(required)}"
    )
