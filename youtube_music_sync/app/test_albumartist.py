import struct

from mutagen.flac import FLAC

import albumartist
from albumartist import VARIOUS_ARTISTS, assign, decide


def test_identical_artists_become_the_album_artist():
    assert decide([["Calibre", "High Contrast"], ["Calibre", "High Contrast"]]) == ["Calibre", "High Contrast"]


def test_a_shared_lead_artist_wins_over_guests():
    assert decide([["Toroki"], ["Toroki", "Isayah"], ["Toroki", "PiyaZawa"]]) == ["Toroki"]


def test_nothing_in_common_is_a_compilation():
    assert decide([["DJ Ransome"], ["Calibre"]]) == [VARIOUS_ARTISTS]


def test_no_artist_tags_means_no_decision():
    assert decide([[], []]) is None


def test_same_titled_singles_from_different_years_stay_apart():
    tracks = [("a", "2022", ["Ed Solo", "Deekline"]), ("b", "2019", ["Friction", "Kanine"])]
    assert assign(tracks) == {"a": ["Ed Solo", "Deekline"], "b": ["Friction", "Kanine"]}


def test_a_compilation_spread_over_years_stays_one_album():
    tracks = [("a", "2019", ["X"]), ("b", "2019", ["Y"]), ("c", "2020", ["Z"])]
    assert set(map(tuple, assign(tracks).values())) == {(VARIOUS_ARTISTS,)}


def _flac(path, **tags):
    """A header-only FLAC file: enough for mutagen to read and write tags."""
    info = struct.pack(">HH", 4096, 4096) + b"\0" * 6
    info += ((44100 << 44) | (1 << 41) | (15 << 36)).to_bytes(8, "big") + b"\0" * 16
    with open(path, "wb") as fh:
        fh.write(b"fLaC" + bytes([0x80]) + len(info).to_bytes(3, "big") + info)
    audio = FLAC(path)
    audio.add_tags()
    for key, value in tags.items():
        audio[key] = value
    audio.save()
    return str(path)


def test_apply_for_rewrites_the_new_track_and_its_siblings(tmp_path):
    old = _flac(tmp_path / "old.flac", album="Sessions", artist="Calibre", date="2020")
    other = _flac(tmp_path / "other.flac", album="Elsewhere", artist="Calibre")
    new = _flac(tmp_path / "new.flac", album="Sessions", artist="DJ Ransome", date="2020")

    assert albumartist.apply_for(new, str(tmp_path)) == 2
    assert FLAC(old)["albumartist"] == [VARIOUS_ARTISTS]
    assert FLAC(new)["albumartist"] == [VARIOUS_ARTISTS]
    assert "albumartist" not in FLAC(other)


def test_apply_for_leaves_a_track_without_an_album_alone(tmp_path):
    path = _flac(tmp_path / "x.flac", artist="Calibre")
    assert albumartist.apply_for(path, str(tmp_path)) == 0
    assert "albumartist" not in FLAC(path)
