"""Normalization is the foundation of duplicate detection: if it over-strips,
different recordings collapse onto one key and music goes missing."""

import pytest

from normalize import (
    VERSION_MARKERS,
    normalize_title,
    similarity,
    tokenize,
    version_conflict,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Some Band - Song Name (Official Music Video)", "some band song name"),
        ("Some Band - Song Name [Official Audio]", "some band song name"),
        ("SONG NAME | Official Lyric Video", "song name"),
        ("Song Name (Official Video) [HD]", "song name"),
        ("Song Name (Remastered 2011)", "song name"),
        ("Song Name (1991)", "song name"),
        ("Song Name HD", "song name"),
        ("01 - Song Name", "song name"),
        ("07. Song Name", "song name"),
        ("Song Name - Topic", "song name"),
        ("Sóng Námé", "song name"),
        ("Artist & Other - Song Name", "artist and other song name"),
        ("Artist feat. Other - Song", "artist feat other song"),
        ("Artist ft Other - Song", "artist feat other song"),
        ("Song Name (Official Video) (Lyrics) [4K]", "song name"),
        ("", ""),
        ("(Official Video)", ""),
    ],
)
def test_noise_is_stripped(raw, expected):
    assert normalize_title(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Song Name (Live)", "song name live"),
        ("Song Name (Live at Wembley)", "song name live at wembley"),
        ("Song Name (Acoustic Version)", "song name acoustic version"),
        ("Song Name (Remix)", "song name remix"),
        ("Song Name (Extended Mix)", "song name extended mix"),
        ("Song Name (Part 2)", "song name part 2"),
        # A version marker inside an otherwise noisy bracket keeps the bracket.
        ("Song Name (Official Live Video)", "song name official live"),
    ],
)
def test_version_markers_survive(raw, expected):
    assert normalize_title(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Regression: these all end in a word that reads as packaging in a
        # bracket but is an ordinary title word in final position. Stripping
        # them collapsed distinct titles onto one key.
        ("Artist feat. Other - Song", "artist feat other song"),
        ("Artist - Love Song", "artist love song"),
        ("Artist - Title Track", "artist title track"),
        ("Artist - Make Music", "artist make music"),
        ("Song", "song"),
        ("Video", "video"),
    ],
)
def test_ordinary_title_words_are_kept(raw, expected):
    assert normalize_title(raw) == expected


def test_every_version_marker_survives_normalization():
    # A marker that normalization mangled would silently stop vetoing
    # duplicates, so assert the whole set round-trips.
    for marker in VERSION_MARKERS:
        assert marker in normalize_title(f"Song Name ({marker})").split()


def test_version_conflict_is_asymmetry_in_markers():
    assert version_conflict(tokenize("song live"), tokenize("song"))
    assert version_conflict(tokenize("song remix"), tokenize("song live"))
    assert not version_conflict(tokenize("song"), tokenize("song name"))
    assert not version_conflict(tokenize("song live"), tokenize("song live 1999"))


def test_similarity_exact_and_contained():
    assert similarity("song name", "song name") == 1.0
    # "Artist - Song" in the library vs "Song" from YouTube, and vice versa.
    assert similarity("some band song name", "song name") == 1.0
    assert similarity("song name", "some band song name") == 1.0


def test_similarity_ignores_single_token_containment():
    # "hello" inside "hello world" must not score as a full match, or every
    # one-word title would match half the library.
    assert similarity("hello", "hello world") < 0.87


def test_similarity_floor_for_unrelated_titles():
    assert similarity("totally different thing", "song name") < 0.5
    assert similarity("", "song name") == 0.0
