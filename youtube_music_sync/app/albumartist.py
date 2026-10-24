"""Choosing an album artist for downloaded tracks.

YouTube gives no album artist, so a library with none files every album under
one fallback artist (Music Assistant uses "Various Artists"). The rule here
works it out from every track in the library that carries the same album tag:

- every track has the same artist(s)        -> those artists
- one artist is credited on every track     -> that (first) artist
- nothing in common                         -> "Various Artists"

YouTube album tags are just titles, so two unrelated singles called "Your
Love" share one. When the album has nothing in common but each release year
does, the year groups are treated as separate albums.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from library import iter_audio_files

_LOG = logging.getLogger(__name__)

VARIOUS_ARTISTS = "Various Artists"


def decide(artist_sets: list[list[str]]) -> list[str] | None:
    """The album artist for tracks with these artist lists, or None if unknown."""
    sets = [s for s in artist_sets if s]
    if not sets:
        return None
    if all(s == sets[0] for s in sets):
        return list(sets[0])
    common = [a for a in sets[0] if all(a in s for s in sets)]
    return [common[0]] if common else [VARIOUS_ARTISTS]


def assign(tracks: list[tuple[str, str, list[str]]]) -> dict[str, list[str]]:
    """Map each (path, year, artists) track of one album title to its album artist."""
    whole = decide([t[2] for t in tracks])
    if whole is None:
        return {}
    if whole == [VARIOUS_ARTISTS]:
        by_year: dict[str, list] = defaultdict(list)
        for t in tracks:
            by_year[t[1]].append(t)
        picks = {year: decide([t[2] for t in group]) for year, group in by_year.items()}
        if len(by_year) > 1 and all(p not in (None, [VARIOUS_ARTISTS]) for p in picks.values()):
            return {t[0]: picks[t[1]] for t in tracks}
    return {t[0]: whole for t in tracks}


def _read(path: str):
    from mutagen import File as MutagenFile

    try:
        return MutagenFile(path, easy=True)
    except Exception as err:
        _LOG.debug("mutagen could not open %s: %s", path, err)
        return None


def apply_for(path: str, music_dir: str, skip_dirs: frozenset[str] = frozenset()) -> int:
    """Set the album artist on `path` and every library track on the same album.

    A new track can turn a one-artist album into a compilation, so its
    siblings are rewritten too. Returns how many files were changed.
    """
    audio = _read(path)
    if audio is None or audio.tags is None or not audio.tags.get("album"):
        return 0
    album = audio.tags["album"][0]

    tracks = []
    for other in iter_audio_files(music_dir, skip_dirs):
        tagged = audio if other == path else _read(other)
        if tagged is None or tagged.tags is None:
            continue
        if (tagged.tags.get("album") or [None])[0] != album:
            continue
        year = (tagged.tags.get("date") or [""])[0][:4]
        tracks.append((other, year, list(tagged.tags.get("artist") or [])))

    changed = 0
    for other, album_artist in assign(tracks).items():
        tagged = audio if other == path else _read(other)
        if tagged is None or list(tagged.tags.get("albumartist") or []) == album_artist:
            continue
        try:
            tagged.tags["albumartist"] = album_artist
            tagged.save()
            changed += 1
        except Exception as err:
            _LOG.warning("Could not set the album artist on %s: %s", other, err)
    return changed
