"""Indexing the existing music library, and deciding what is already in it.

Downloads land straight into a curated library, so the expensive mistake is
adding a second copy of a track under a slightly different name. The cheap
mistake is skipping something and having to fetch it by hand. The matcher is
tuned accordingly: it needs *both* a title match and a duration match before
it will call something a duplicate.

Four verdicts come out of `LibraryIndex.match`:

NEW                nothing close - download it.
DUPLICATE          title and duration both match - skip without downloading.
DIFFERENT_VERSION  title matches but duration or version markers do not. Not a
                   duplicate (a live take is not the studio cut), but worth
                   reporting so a genuine near-miss can be eyeballed.
AMBIGUOUS          title matches, duration unknown on one side. Download, then
                   re-check with the real duration in hand.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from normalize import normalize_title, similarity, tokenize, version_conflict

_LOG = logging.getLogger(__name__)

AUDIO_EXTENSIONS = frozenset(
    {".mp3", ".m4a", ".m4b", ".mp4a", ".aac", ".opus", ".ogg", ".oga", ".flac",
     ".wav", ".wma", ".alac", ".aiff", ".aif", ".ape", ".mka"}
)

# Tag key this add-on stamps its own downloads with, so a video is recognised
# as already-owned even if the state database is lost or the file is renamed.
VIDEO_ID_TAG = "YOUTUBE_ID"

# A trailing "[dQw4w9WgXcQ]"-style group, as yt-dlp's %(id)s templates produce.
_FILENAME_VIDEO_ID = re.compile(r"[\[(]([A-Za-z0-9_-]{11})[\])]\s*$")

NEW = "new"
DUPLICATE = "duplicate"
DIFFERENT_VERSION = "different_version"
AMBIGUOUS = "ambiguous"

@dataclass(frozen=True)
class TitleVariant:
    """One comparable name for a file (from its filename, or from its tags)."""

    key: str
    tokens: frozenset[str]
    source: str

    @classmethod
    def build(cls, raw: str, source: str) -> "TitleVariant | None":
        key = normalize_title(raw)
        if not key:
            return None
        return cls(key=key, tokens=tokenize(key), source=source)


@dataclass(frozen=True)
class LibraryEntry:
    path: str
    duration: float | None
    video_id: str | None
    variants: tuple[TitleVariant, ...]


@dataclass(frozen=True)
class MatchResult:
    status: str
    path: str | None = None
    score: float = 0.0
    reason: str = ""

    @property
    def is_duplicate(self) -> bool:
        return self.status == DUPLICATE


def probe_duration(path: str | os.PathLike[str]) -> float | None:
    """Duration in seconds, via mutagen, falling back to ffprobe."""
    try:
        from mutagen import File as MutagenFile

        audio = MutagenFile(str(path))
        if audio is not None and audio.info is not None:
            length = getattr(audio.info, "length", None)
            if length:
                return float(length)
    except Exception as err:  # unreadable tags must not abort a whole scan
        _LOG.debug("mutagen could not read %s: %s", path, err)

    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=60, check=False,
        )
        if out.returncode == 0 and out.stdout.strip():
            return float(out.stdout.strip())
    except Exception as err:
        _LOG.debug("ffprobe could not read %s: %s", path, err)
    return None


def _tag_values(audio, *keys: str) -> list[str]:
    values: list[str] = []
    for key in keys:
        try:
            raw = audio.get(key)
        except Exception:
            continue
        if not raw:
            continue
        if isinstance(raw, (list, tuple)):
            items = raw
        else:
            items = [raw]
        for item in items:
            text = item.decode("utf-8", "replace") if isinstance(item, bytes) else str(item)
            text = text.strip()
            if text:
                values.append(text)
    return values


def read_file_metadata(path: str | os.PathLike[str]) -> tuple[float | None, str | None, tuple[TitleVariant, ...]]:
    """Duration, stamped video id and comparable titles for one library file.

    Both the filename and the tags are turned into variants, because a library
    assembled over years has some files named "Artist - Title" and others named
    "Title" with the artist only in the tags. Matching against every variant and
    keeping the best score is what makes the filename check tolerant.
    """
    path = Path(path)
    duration: float | None = None
    video_id: str | None = None
    variants: list[TitleVariant] = []

    stem = TitleVariant.build(path.stem, "filename")
    if stem:
        variants.append(stem)

    filename_id = _FILENAME_VIDEO_ID.search(path.stem)
    if filename_id:
        video_id = filename_id.group(1)

    try:
        from mutagen import File as MutagenFile

        audio = MutagenFile(str(path))
    except Exception as err:
        _LOG.debug("mutagen could not open %s: %s", path, err)
        audio = None

    if audio is not None:
        if audio.info is not None:
            length = getattr(audio.info, "length", None)
            if length:
                duration = float(length)

        titles = _tag_values(audio, "title", "TIT2", "\xa9nam")
        artists = _tag_values(audio, "artist", "TPE1", "\xa9ART", "albumartist", "TPE2")
        if titles:
            tagged = TitleVariant.build(titles[0], "tag:title")
            if tagged:
                variants.append(tagged)
            if artists:
                combined = TitleVariant.build(f"{artists[0]} - {titles[0]}", "tag:artist-title")
                if combined:
                    variants.append(combined)

        if not video_id:
            stamped = _tag_values(
                audio,
                VIDEO_ID_TAG, VIDEO_ID_TAG.lower(),
                f"TXXX:{VIDEO_ID_TAG}",
                f"----:com.apple.iTunes:{VIDEO_ID_TAG}",
            )
            for value in stamped:
                candidate = value.strip()
                if len(candidate) == 11:
                    video_id = candidate
                    break

    if duration is None:
        duration = probe_duration(path)

    # De-duplicate variants by key, keeping first (filename) occurrence.
    unique: dict[str, TitleVariant] = {}
    for variant in variants:
        unique.setdefault(variant.key, variant)
    return duration, video_id, tuple(unique.values())


def variants_to_json(variants: tuple[TitleVariant, ...]) -> str:
    return json.dumps([[v.key, v.source] for v in variants])


def variants_from_json(blob: str) -> tuple[TitleVariant, ...]:
    try:
        rows = json.loads(blob)
    except (TypeError, ValueError):
        return ()
    out = []
    for row in rows:
        if isinstance(row, (list, tuple)) and row and row[0]:
            key = str(row[0])
            source = str(row[1]) if len(row) > 1 else "cached"
            out.append(TitleVariant(key=key, tokens=tokenize(key), source=source))
    return tuple(out)


class LibraryIndex:
    """In-memory index of library files, searchable by token overlap."""

    def __init__(self, entries: list[LibraryEntry] | None = None) -> None:
        self._entries: list[LibraryEntry] = []
        self._by_token: dict[str, set[int]] = {}
        self._by_video_id: dict[str, LibraryEntry] = {}
        for entry in entries or []:
            self.add(entry)

    def add(self, entry: LibraryEntry) -> None:
        index = len(self._entries)
        self._entries.append(entry)
        for variant in entry.variants:
            for token in variant.tokens:
                self._by_token.setdefault(token, set()).add(index)
        if entry.video_id:
            self._by_video_id.setdefault(entry.video_id, entry)

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def entries(self) -> tuple[LibraryEntry, ...]:
        return tuple(self._entries)

    def match(
        self,
        title: str,
        duration: float | None,
        *,
        video_id: str | None = None,
        tolerance: float = 5.0,
        threshold: float = 0.87,
    ) -> MatchResult:
        """Decide whether `title`/`duration` is already in the library."""
        if video_id and video_id in self._by_video_id:
            entry = self._by_video_id[video_id]
            return MatchResult(DUPLICATE, entry.path, 1.0,
                               f"YouTube id {video_id} is already tagged on this file")

        key = normalize_title(title)
        if not key:
            return MatchResult(NEW, reason="title normalized to nothing")
        tokens = tokenize(key)

        candidates: set[int] = set()
        for token in tokens:
            hits = self._by_token.get(token)
            if hits:
                candidates |= hits

        best_dup: tuple[float, LibraryEntry, str] | None = None
        best_amb: tuple[float, LibraryEntry, str] | None = None
        best_ver: tuple[float, LibraryEntry, str] | None = None

        for index in candidates:
            entry = self._entries[index]
            for variant in entry.variants:
                score = similarity(key, variant.key)
                if score < threshold:
                    continue

                if version_conflict(tokens, variant.tokens):
                    marker_diff = sorted((tokens | variant.tokens) & _version_markers())
                    reason = (f"title matches but version markers differ "
                              f"({', '.join(marker_diff)})")
                    if best_ver is None or score > best_ver[0]:
                        best_ver = (score, entry, reason)
                    continue

                if duration is None or entry.duration is None:
                    # With no duration to corroborate it, only an exact key
                    # match is conclusive. Token containment can score a
                    # perfect 1.0 on titles that merely share every word of
                    # the shorter one ("Other Song" inside "Other Song
                    # Returns"), which is not enough on its own.
                    if variant.key == key:
                        reason = "identical title, no duration available to check"
                        if best_dup is None or score > best_dup[0]:
                            best_dup = (score, entry, reason)
                    else:
                        reason = "similar title, no duration available to check"
                        if best_amb is None or score > best_amb[0]:
                            best_amb = (score, entry, reason)
                    continue

                delta = abs(duration - entry.duration)
                if delta <= tolerance:
                    reason = (f"title {score:.0%} match and duration within "
                              f"{delta:.0f}s")
                    if best_dup is None or score > best_dup[0]:
                        best_dup = (score, entry, reason)
                else:
                    reason = (f"title {score:.0%} match but duration differs by "
                              f"{delta:.0f}s")
                    if best_ver is None or score > best_ver[0]:
                        best_ver = (score, entry, reason)

        if best_dup:
            score, entry, reason = best_dup
            return MatchResult(DUPLICATE, entry.path, score, reason)
        if best_amb:
            score, entry, reason = best_amb
            return MatchResult(AMBIGUOUS, entry.path, score, reason)
        if best_ver:
            score, entry, reason = best_ver
            return MatchResult(DIFFERENT_VERSION, entry.path, score, reason)
        return MatchResult(NEW, reason="no similar title in library")


def _version_markers() -> frozenset[str]:
    from normalize import VERSION_MARKERS

    return VERSION_MARKERS


def iter_audio_files(root: str | os.PathLike[str], skip_dirs: frozenset[str] = frozenset()):
    """Walk `root` yielding audio files, skipping hidden and excluded folders."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith(".") and os.path.join(dirpath, d) not in skip_dirs
        ]
        for filename in filenames:
            if filename.startswith("."):
                continue
            if Path(filename).suffix.lower() in AUDIO_EXTENSIONS:
                yield os.path.join(dirpath, filename)
