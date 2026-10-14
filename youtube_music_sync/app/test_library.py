"""The matcher has to be right in both directions: never silently treat a
different recording as owned, never re-download something already there."""

import os
import shutil
import subprocess

import pytest

import library
from library import (
    AMBIGUOUS,
    DIFFERENT_VERSION,
    DUPLICATE,
    NEW,
    LibraryEntry,
    LibraryIndex,
    TitleVariant,
    iter_audio_files,
    read_file_metadata,
    variants_from_json,
    variants_to_json,
)

HAVE_FFMPEG = shutil.which("ffmpeg") is not None


def entry(path, duration, *names, video_id=None):
    variants = tuple(v for v in (TitleVariant.build(n, "test") for n in names) if v)
    return LibraryEntry(path=path, duration=duration, video_id=video_id, variants=variants)


@pytest.fixture
def index():
    return LibraryIndex([
        entry("/m/Some Band - Song Name.mp3", 215.0, "Some Band - Song Name"),
        entry("/m/Other Song.mp3", 180.0, "Other Song"),
        entry("/m/tagged.mp3", 200.0, "tagged", "Third Artist - Third Song",
              video_id="abc12345678"),
    ])


def test_title_and_duration_match_is_a_duplicate(index):
    result = index.match("Some Band - Song Name (Official Video)", 217.0)
    assert result.status == DUPLICATE
    assert result.path == "/m/Some Band - Song Name.mp3"
    assert result.is_duplicate


def test_duration_outside_tolerance_is_not_a_duplicate(index):
    result = index.match("Some Band - Song Name", 260.0)
    assert result.status == DIFFERENT_VERSION
    assert "duration differs by 45s" in result.reason


def test_tolerance_is_configurable(index):
    assert index.match("Some Band - Song Name", 225.0).status == DIFFERENT_VERSION
    assert index.match("Some Band - Song Name", 225.0, tolerance=15).status == DUPLICATE


def test_version_marker_vetoes_duplicate_even_with_matching_duration(index):
    # The live cut of a track the library already has, at almost the same
    # length. Title and duration both "match" - the marker is the only signal.
    result = index.match("Some Band - Song Name (Live)", 215.0)
    assert result.status == DIFFERENT_VERSION
    assert "version markers differ" in result.reason


def test_unrelated_title_is_new(index):
    assert index.match("Brand New Thing", 200.0).status == NEW


def test_identical_title_without_duration_is_a_duplicate(index):
    result = index.match("Other Song", None)
    assert result.status == DUPLICATE


def test_similar_title_without_duration_is_ambiguous(index):
    # Worth downloading, then re-checking once the real duration is known.
    result = index.match("Other Song Returns", None)
    assert result.status == AMBIGUOUS


def test_video_id_tag_short_circuits(index):
    result = index.match("A Completely Different Name", 999.0, video_id="abc12345678")
    assert result.status == DUPLICATE
    assert result.path == "/m/tagged.mp3"


def test_matching_uses_tag_variants_not_just_filenames(index):
    # The file on disk is "tagged.mp3"; only its tags say what it is.
    result = index.match("Third Artist - Third Song (Official Video)", 201.0)
    assert result.status == DUPLICATE
    assert result.path == "/m/tagged.mp3"


def test_threshold_is_respected(index):
    loose = index.match("Some Band - Song Nam", 215.0, threshold=0.5)
    strict = index.match("Some Band - Song Nam", 215.0, threshold=0.999)
    assert loose.status == DUPLICATE
    assert strict.status == NEW


def test_empty_index_calls_everything_new():
    assert LibraryIndex().match("Anything", 100.0).status == NEW


def test_variants_json_round_trip():
    variants = (TitleVariant.build("Some Band - Song Name", "filename"),)
    restored = variants_from_json(variants_to_json(variants))
    assert restored[0].key == variants[0].key
    assert restored[0].tokens == variants[0].tokens
    assert variants_from_json("not json") == ()


def test_iter_audio_files_filters(tmp_path):
    (tmp_path / "a.mp3").write_bytes(b"x")
    (tmp_path / "b.opus").write_bytes(b"x")
    (tmp_path / "notes.txt").write_bytes(b"x")
    (tmp_path / ".hidden.mp3").write_bytes(b"x")
    (tmp_path / ".cache").mkdir()
    (tmp_path / ".cache" / "c.mp3").write_bytes(b"x")
    quarantine = tmp_path / ".yt_duplicates"
    quarantine.mkdir()
    (quarantine / "d.mp3").write_bytes(b"x")
    nested = tmp_path / "Album"
    nested.mkdir()
    (nested / "e.flac").write_bytes(b"x")

    found = {os.path.basename(p) for p in iter_audio_files(tmp_path)}
    assert found == {"a.mp3", "b.opus", "e.flac"}

    skipped = {os.path.basename(p) for p in iter_audio_files(
        tmp_path, skip_dirs=frozenset({str(nested)}))}
    assert skipped == {"a.mp3", "b.opus"}


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg is needed to synthesise test audio")
@pytest.mark.parametrize("name", ["tone.flac", "tone.m4a"])
def test_read_file_metadata_on_real_audio(tmp_path, name):
    path = tmp_path / name
    assert subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=3", str(path)],
        check=False,
    ).returncode == 0

    duration, video_id, variants = read_file_metadata(path)
    assert duration == pytest.approx(3.0, abs=0.5)
    assert video_id is None
    assert variants and variants[0].key == "tone"


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg is needed to synthesise test audio")
@pytest.mark.parametrize("name", ["tone.flac", "tone.m4a", "tone.ogg"])
def test_stamped_video_id_is_read_back(tmp_path, name):
    import downloader

    path = tmp_path / name
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=2", str(path)],
        check=False,
    )
    assert downloader.stamp_video_id(str(path), "dQw4w9WgXcQ")

    # This is what makes dedup survive a lost database or a renamed file.
    _duration, video_id, _variants = read_file_metadata(path)
    assert video_id == "dQw4w9WgXcQ"


def test_video_id_recovered_from_filename(tmp_path):
    path = tmp_path / "Some Band - Song Name [dQw4w9WgXcQ].mp3"
    path.write_bytes(b"not really audio")
    _duration, video_id, variants = read_file_metadata(path)
    assert video_id == "dQw4w9WgXcQ"
    assert variants  # the filename still yields a comparable title


def test_unreadable_file_does_not_raise(tmp_path):
    path = tmp_path / "broken.mp3"
    path.write_bytes(b"this is not audio at all")
    duration, video_id, variants = read_file_metadata(path)
    assert duration is None and video_id is None
    assert variants[0].key == "broken"
