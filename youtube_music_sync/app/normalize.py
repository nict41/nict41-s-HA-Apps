"""Turning messy track titles into comparable keys.

A YouTube title ("Some Band - Song Name (Official Music Video) [HD]") and the
name of the same track already sitting in a music library ("01 - Some Band -
Song Name.mp3", or tags reading artist="Some Band", title="Song Name") have to
collapse onto the same string before they can be compared.

Two rules shape what gets stripped:

* Packaging noise is removed. "(Official Video)", "[HD]", "| Lyrics" and
  friends say nothing about which recording this is.
* Version markers are *kept*, and a mismatch in them blocks a duplicate
  verdict outright. "(Live)", "(Remix)" and "(Acoustic)" are genuinely
  different recordings, and silently treating a remix as a duplicate of the
  studio cut is the one failure mode that loses music without telling anyone.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

# Words that mark a bracketed segment as packaging rather than part of the
# track name. A bracket is dropped when *every* word inside it is noise (or a
# throwaway connective), so "(Official Video)" goes and "(Live in Tokyo)"
# stays.
NOISE_WORDS = frozenset(
    """
    official officiel oficial audio video vid visual visualizer visualiser
    lyric lyrics lyricvideo subtitled subtitles captions
    hd hq sd uhd 4k 8k 1080p 720p 480p 60fps
    mv pv full quality highquality
    remaster remastered remasterizado
    explicit clean censored uncensored
    free download downloadlink out now new latest
    music song track audioonly onlyaudio
    stream streaming premiere
    """.split()
)

# Connectives that do not by themselves make a bracket meaningful.
_FILLER_WORDS = frozenset({"the", "a", "an", "in", "of", "and", "with", "version", "ver"})

# Words safe to drop when they trail a title outside any bracket. Deliberately
# much narrower than NOISE_WORDS: "song", "track" and "music" are ordinary
# title words, and stripping them would collapse "Love Song" onto "Love". These
# are only ever format or packaging labels in final position.
_TRAILING_NOISE = frozenset(
    """
    hd hq sd uhd 4k 8k 1080p 720p 480p 60fps mv pv
    official lyrics lyric audio video visualizer visualiser
    remaster remastered explicit censored uncensored
    """.split()
)

# Markers that distinguish one recording of a song from another. These survive
# normalization, and `version_conflict` uses them to veto duplicate verdicts.
VERSION_MARKERS = frozenset(
    """
    live remix acoustic instrumental karaoke unplugged demo cover
    extended edit mix radioedit club dub bootleg mashup
    reprise session sessions rehearsal soundcheck
    slowed sped reverb nightcore 8d
    orchestral piano strings symphonic
    intro outro interlude snippet preview teaser
    """.split()
)

_DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−⁃"), "-")
_QUOTES = dict.fromkeys(map(ord, "‘’‚‛′´`"), "'")
_DQUOTES = dict.fromkeys(map(ord, "“”„‟″"), '"')

_BRACKETS = (("(", ")"), ("[", "]"), ("{", "}"), ("（", "）"))

_LEADING_TRACK_NO = re.compile(r"^\s*\d{1,3}\s*(?:[-._)\]]|–|—)\s+")
_FEAT = re.compile(r"\b(?:feat|ft|featuring|f)\s*\.?\s+", re.IGNORECASE)
_YT_TOPIC = re.compile(r"\s*-\s*topic\s*$", re.IGNORECASE)
_NON_WORD = re.compile(r"[^a-z0-9 ]+")
_WS = re.compile(r"\s+")


def _strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _words(segment: str) -> list[str]:
    return [w for w in _NON_WORD.sub(" ", segment.lower()).split() if w]


def _is_noise_segment(segment: str) -> bool:
    """True when a bracketed or pipe-delimited segment is pure packaging."""
    words = _words(segment)
    if not words:
        return True
    if any(w in VERSION_MARKERS for w in words):
        return False
    # Bare numbers inside a bracket are annotations, not names: "(Remastered
    # 2011)" and "(1991)" should drop, while "(Part 2)" survives because
    # "part" is not a noise word.
    meaningful = [w for w in words if w not in _FILLER_WORDS and not w.isdigit()]
    if not meaningful:
        return True
    return all(w in NOISE_WORDS for w in meaningful)


def _strip_noise_brackets(text: str) -> str:
    """Drop bracketed groups that contain only packaging words.

    Hand-rolled rather than regex because bracket groups nest and repeat, and
    only the innermost complete group can be judged in isolation.
    """
    changed = True
    while changed:
        changed = False
        for open_ch, close_ch in _BRACKETS:
            start = text.rfind(open_ch)
            while start != -1:
                end = text.find(close_ch, start + 1)
                if end == -1:
                    break
                inner = text[start + 1 : end]
                if open_ch in inner:  # not the innermost group yet
                    break
                if _is_noise_segment(inner):
                    text = text[:start] + " " + text[end + 1 :]
                    changed = True
                    break
                start = text.rfind(open_ch, 0, start)
            if changed:
                break
    return text


def _strip_noise_pipes(text: str) -> str:
    """Drop "| Official Video"-style trailing segments."""
    if "|" not in text:
        return text
    parts = text.split("|")
    kept = [p for p in parts if not _is_noise_segment(p)]
    # Everything looked like noise - the title probably *is* the first segment.
    return " ".join(kept).strip() if kept else parts[0]


def _strip_trailing_noise_words(text: str) -> str:
    """Drop bare trailing packaging words left outside any bracket."""
    words = text.split()
    # Never strip the title down to nothing: a one-word title that happens to
    # be a packaging word is still all the name we have.
    while len(words) > 1:
        tail = _NON_WORD.sub("", words[-1].lower())
        if tail and tail in _TRAILING_NOISE and tail not in VERSION_MARKERS:
            words.pop()
            continue
        break
    return " ".join(words)


def normalize_title(title: str) -> str:
    """Collapse a title to a comparison key: lowercase words, noise removed."""
    if not title:
        return ""
    text = title.translate(_DASHES).translate(_QUOTES).translate(_DQUOTES)
    text = _strip_accents(text)
    text = _YT_TOPIC.sub("", text)
    text = _LEADING_TRACK_NO.sub("", text)
    text = _strip_noise_brackets(text)
    text = _strip_noise_pipes(text)
    text = _FEAT.sub(" feat ", text)
    text = text.replace("&", " and ")
    text = text.lower()
    text = _NON_WORD.sub(" ", text)
    text = _WS.sub(" ", text).strip()
    text = _strip_trailing_noise_words(text)
    return text


def tokenize(key: str) -> frozenset[str]:
    """Token set for the cheap overlap comparison and the version check."""
    return frozenset(key.split())


def version_conflict(a_tokens: frozenset[str], b_tokens: frozenset[str]) -> bool:
    """True when one title claims a version the other does not.

    Studio cut vs live take vs remix: near-identical names, different
    recordings. Any asymmetry in version markers vetoes a duplicate verdict.
    """
    return bool((a_tokens & VERSION_MARKERS) ^ (b_tokens & VERSION_MARKERS))


def similarity(a_key: str, b_key: str) -> float:
    """0..1 similarity blending whole-string ratio with token containment.

    Containment is what catches "Artist - Song" against a library file named
    just "Song": the smaller token set being wholly inside the larger scores 1,
    where a plain character ratio would score ~0.6. It is only trusted from two
    tokens up, since single-word containment matches far too much.
    """
    if not a_key or not b_key:
        return 0.0
    if a_key == b_key:
        return 1.0

    ratio = SequenceMatcher(None, a_key, b_key).ratio()

    a_tokens, b_tokens = tokenize(a_key), tokenize(b_key)
    smaller = min(len(a_tokens), len(b_tokens))
    containment = 0.0
    if smaller >= 2:
        containment = len(a_tokens & b_tokens) / smaller

    return max(ratio, containment)
