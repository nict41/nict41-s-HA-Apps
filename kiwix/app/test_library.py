from pathlib import Path

import library
import settings


def _make_zim(directory: str, name: str, size: int = 32) -> Path:
    path = Path(directory) / name
    path.write_bytes(b"z" * size)
    return path


def test_missing_share_lists_nothing_instead_of_raising(monkeypatch):
    monkeypatch.setenv("ZIM_PATH", "GhostNAS/Kiwix")
    assert library.list_zims() == []
    assert library.served_paths() == []


def test_lists_complete_and_partial_archives(zim_dir):
    _make_zim(zim_dir, "wikipedia_en_mini.zim", 64)
    _make_zim(zim_dir, "wikipedia_en_maxi.zim.part", 16)

    entries = {entry["filename"]: entry for entry in library.list_zims()}

    assert entries["wikipedia_en_mini.zim"]["complete"] is True
    assert entries["wikipedia_en_mini.zim"]["size"] == 64
    assert entries["wikipedia_en_maxi.zim"]["complete"] is False
    assert entries["wikipedia_en_maxi.zim"]["served"] is False


def test_serving_selection_persists_and_resolves_to_a_path(zim_dir):
    _make_zim(zim_dir, "wikipedia_en_mini.zim")

    library.set_served("wikipedia_en_mini.zim", True)

    assert library.read_state()["served"] == ["wikipedia_en_mini.zim"]
    assert [p.name for p in library.served_paths()] == ["wikipedia_en_mini.zim"]

    library.set_served("wikipedia_en_mini.zim", False)
    assert library.read_state()["served"] == []


def test_selection_survives_the_share_going_away(zim_dir, monkeypatch):
    _make_zim(zim_dir, "wikipedia_en_mini.zim")
    library.set_served("wikipedia_en_mini.zim", True)

    monkeypatch.setenv("ZIM_PATH", "GhostNAS/Kiwix")
    library.prune_served()

    # An unmounted share is not evidence the archive was deleted.
    assert library.read_state()["served"] == ["wikipedia_en_mini.zim"]


def test_prune_drops_selections_whose_file_is_gone(zim_dir):
    path = _make_zim(zim_dir, "wikipedia_en_mini.zim")
    library.set_served("wikipedia_en_mini.zim", True)

    path.unlink()
    library.prune_served()

    assert library.read_state()["served"] == []


def test_path_traversal_is_refused(zim_dir):
    assert library.zim_path_for("../../etc/passwd.zim") is None
    assert library.zim_path_for("notes.txt") is None
    assert library.zim_path_for(".hidden.zim") is None
    assert library.zim_path_for("wikipedia_en_mini.zim") == Path(zim_dir) / "wikipedia_en_mini.zim"


def test_delete_removes_the_archive_its_partial_and_its_selection(zim_dir):
    _make_zim(zim_dir, "wikipedia_en_mini.zim")
    _make_zim(zim_dir, "wikipedia_en_mini.zim.part")
    library.set_served("wikipedia_en_mini.zim", True)

    ok, error = library.delete_zim("wikipedia_en_mini.zim")

    assert (ok, error) == (True, "")
    assert not (Path(zim_dir) / "wikipedia_en_mini.zim").exists()
    assert not (Path(zim_dir) / "wikipedia_en_mini.zim.part").exists()
    assert library.read_state()["served"] == []


def test_deleting_something_absent_is_an_error_not_a_crash(zim_dir):
    ok, error = library.delete_zim("nope.zim")
    assert ok is False
    assert "not in the library" in error


def test_empty_selection_still_writes_a_valid_library(zim_dir):
    library.rebuild_library_xml()
    assert "<library" in library.library_xml().read_text()
