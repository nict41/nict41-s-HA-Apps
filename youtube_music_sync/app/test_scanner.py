"""The scan cache is what keeps a rescan over SMB cheap - and a stale entry
there would mean comparing against a file that no longer says what it used to."""

import os
import shutil
import subprocess

import pytest

from scanner import build_index
from settings import Config
from state import Store

HAVE_FFMPEG = shutil.which("ffmpeg") is not None
pytestmark = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg is needed to synthesise test audio")


def make_audio(path, seconds=3):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", f"sine=frequency=440:duration={seconds}", str(path)],
        check=True,
    )


@pytest.fixture
def library(tmp_path):
    music = tmp_path / "music"
    make_audio(str(music / "Some Band - Song Name.flac"), 3)
    make_audio(str(music / "Album" / "02 - Other Song.flac"), 4)
    (music / "cover.jpg").write_bytes(b"not audio")
    config = Config(playlists=("u",), music_dir=str(music), data_dir=str(tmp_path / "data"))
    return config, Store(config.db_path)


def test_first_scan_reads_everything(library):
    config, store = library
    index, stats = build_index(config, store)

    assert stats.files == 2
    assert stats.reread == 2
    assert len(index) == 2
    assert index.match("Some Band - Song Name (Official Video)", 3.0).is_duplicate


def test_second_scan_reuses_the_cache(library):
    config, store = library
    build_index(config, store)
    index, stats = build_index(config, store)

    # Nothing changed on disk, so nothing should be read again.
    assert stats.files == 2 and stats.reread == 0 and stats.pruned == 0
    assert len(index) == 2
    assert index.match("Some Band - Song Name", 3.0).is_duplicate


def test_changed_file_is_re_read(library):
    config, store = library
    build_index(config, store)

    # Same path, different content and length.
    make_audio(os.path.join(config.music_dir, "Some Band - Song Name.flac"), 9)
    index, stats = build_index(config, store)

    assert stats.reread == 1
    # The duration the matcher uses must follow the file, not the stale cache.
    assert index.match("Some Band - Song Name", 9.0).is_duplicate
    assert not index.match("Some Band - Song Name", 3.0).is_duplicate


def test_deleted_file_is_pruned(library):
    config, store = library
    build_index(config, store)

    os.remove(os.path.join(config.music_dir, "Album", "02 - Other Song.flac"))
    index, stats = build_index(config, store)

    assert stats.files == 1 and stats.pruned == 1
    assert len(index) == 1
    assert store.stats()["library_files"] == 1
    # No longer owned, so it should be downloadable again.
    assert index.match("Other Song", 4.0).status == "new"


def test_new_file_is_picked_up(library):
    config, store = library
    build_index(config, store)

    make_audio(os.path.join(config.music_dir, "Third Track.flac"), 5)
    index, stats = build_index(config, store)

    assert stats.files == 3 and stats.reread == 1
    assert index.match("Third Track", 5.0).is_duplicate


def test_quarantined_duplicates_are_not_indexed(library):
    config, store = library
    make_audio(os.path.join(config.quarantine_dir, "Rejected.flac"), 3)

    index, stats = build_index(config, store)

    # Indexing quarantine would make every later copy look like a duplicate
    # of a file the user has already rejected.
    assert stats.files == 2
    assert index.match("Rejected", 3.0).status == "new"


def test_missing_music_dir_yields_an_empty_index(tmp_path):
    config = Config(playlists=("u",), music_dir=str(tmp_path / "nope"),
                    data_dir=str(tmp_path / "data"))
    index, stats = build_index(config, Store(config.db_path))
    assert len(index) == 0 and stats.files == 0


def test_playlist_folders_are_indexed_too(tmp_path):
    from settings import PlaylistSetting
    music, talks = tmp_path / "music", tmp_path / "talks"
    make_audio(str(music / "Song.flac"), 3)
    make_audio(str(talks / "Talk.flac"), 4)
    config = Config(playlists=("u",), music_dir=str(music), data_dir=str(tmp_path / "data"),
                    playlist_settings=(PlaylistSetting("PLtalks", folder=str(talks)),))
    index, stats = build_index(config, Store(config.db_path))
    assert index.match("Talk", 4.0).is_duplicate
