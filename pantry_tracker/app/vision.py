"""Reads use-by/best-before dates. Text from either reader passes through
parse_transcript here: the on-device OCR running in the browser sends what
it read to /api/parse-date, and the photo itself goes to a self-hosted
vision-language model, via an Open WebUI / Ollama OpenAI-compatible
`/chat/completions` endpoint (e.g. glm-ocr running locally). The photo only
ever goes to the configured endpoint on the user's own network - never to a
third-party vision API.

Extraction is always best-effort: a model outage, a garbled reply, or a
date it couldn't find all degrade to "no date found" rather than an error,
so the scan flow always falls through to manual entry.
"""

import base64
import calendar
import json
import re
import socket
import urllib.error
import urllib.request
from datetime import date, timedelta
from io import BytesIO

from PIL import Image, ImageOps

import settings

# Long enough for a model that has to load first, which was seen taking most
# of a minute on a shared GPU.
_TIMEOUT_SECONDS = 90
# Photos arrive already cropped to a date, so most of their pixels are the
# print itself - this only reins in an uncropped photo from the file picker.
_MAX_DIMENSION = 2048

# Asks for a copy of the print rather than a formatted date. Told to output
# YYYY-MM-DD, the model invents a year for packaging that doesn't print one
# (2021 and 2023 in real scans), so working out the year is left to
# _resolve_unknown_year below. Copying every line first is what got
# qwen2.5vl to read dot-printed dates it otherwise gave up on. Keep real
# dates out of this: a model will echo an example back as its answer.
_PROMPT = (
    "Read the text printed on this food packaging. It is often made of small "
    "dots or faint ink.\n"
    "First, write out every line of printed text you can see, one per line, "
    "copying the characters exactly.\n"
    "Then finish with one line starting DATE: followed by the use-by or "
    "best-before date copied exactly as printed. Do not add a year if none "
    "is printed. If no date is legible, write DATE: UNKNOWN"
)
# OCR models are driven by a fixed task prompt rather than instructions, so
# this gets a plain transcript back with no DATE: line.
_OCR_MODEL_PROMPT = "Text Recognition:"

_ISO_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
# DD/MM/YYYY, DD-MM-YYYY, DD.MM.YYYY - the convention almost all UK/EU
# packaging prints dates in, which is what this app is primarily built for.
_DMY_RE = re.compile(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\b")
# Packaging also prints these with spaces or nothing between the parts:
# "26 12 26", "0103 27", "010327". The lookarounds keep them from matching
# inside a batch code or a time.
_SPACED_DATE_RE = re.compile(r"(?<![\w:./-])(\d{1,2})[ \t]+(\d{1,2})[ \t]+(\d{4}|\d{2})(?![\w:./-])")
_COMPACT_DATE_RE = re.compile(r"(?<![\w:./-])(\d{2})(\d{2})[ \t]?(\d{4}|\d{2})(?![\w:./-])")
# A bare day and month only counts when it's all a line says, apart from a
# label: elsewhere in a transcript, "2.5 OZ" would be 2 May.
_LABEL_PATTERN = r"(?:use\s*by|best\s*before(?:\s*end)?|bbe|bb|exp(?:iry)?)"
_DM_RE = re.compile(rf"^(?:{_LABEL_PATTERN}\W*)?(\d{{1,2}})[./-](\d{{2}})$", re.IGNORECASE)
_LABEL_RE = re.compile(rf"\b{_LABEL_PATTERN}\b", re.IGNORECASE)
_DATE_LINE_RE = re.compile(r"^\s*DATE:\s*(\S.*)$", re.IGNORECASE | re.MULTILINE)

_MONTH_NAMES = (
    ("jan", "january"),
    ("feb", "february"),
    ("mar", "march"),
    ("apr", "april"),
    ("may",),
    ("jun", "june"),
    ("jul", "july"),
    ("aug", "august"),
    ("sep", "sept", "september"),
    ("oct", "october"),
    ("nov", "november"),
    ("dec", "december"),
)
_MONTHS = {name: index + 1 for index, names in enumerate(_MONTH_NAMES) for name in names}
# Only real month names, so a transcript's "6 MARSHMALLOWS" isn't 6 March.
_MONTH_PATTERN = "(" + "|".join(name for names in _MONTH_NAMES for name in sorted(names, key=len, reverse=True)) + ")(?![a-z])"
_DAY_PATTERN = r"(\d{1,2})(?:st|nd|rd|th)?"
# Spaces are optional because the model sometimes runs the print together,
# e.g. "L0828JAN2702:48". The lookahead stops a time being taken for a year:
# "02 Nov 11:26" is not 2011.
_TEXT_DATE_RE = re.compile(rf"{_DAY_PATTERN}\s*{_MONTH_PATTERN}\s*(\d{{4}}|\d{{2}})(?![\d:])", re.IGNORECASE)
_TEXT_DATE_NO_YEAR_RE = re.compile(rf"{_DAY_PATTERN}\s*{_MONTH_PATTERN}", re.IGNORECASE)
_MONTH_YEAR_TEXT_RE = re.compile(rf"(?<![A-Za-z0-9]){_MONTH_PATTERN}\s+(\d{{2}}|\d{{4}})(?![\d:])", re.IGNORECASE)
_MONTH_YEAR_NUMERIC_RE = re.compile(r"(?<![\w:./-])(0?[1-9]|1[0-2])(?:([/.])(\d{2}|\d{4})| (\d{4}))(?![\w:./-])")

# How far past a date with no year can be and still mean this year's - long
# enough that something bought and forgotten still shows as expired, instead
# of silently gaining a year.
_NO_YEAR_GRACE_DAYS = 30
# Past this, a date with a year is treated as a misread year. Food this old
# isn't being added to a pantry, but a model does sometimes invent a year.
_MAX_PAST_DAYS = 180
# Batch codes can look like spaced or compact dates, so those need to land
# somewhere a use-by date plausibly would.
_MAX_FUTURE_YEARS = 10

SCORE_LABELLED = 8
SCORE_PREVIOUS_LABEL = 2
SCORE_SEPARATORS = 3
SCORE_PRINTED_YEAR = 2
PENALTY_COMPACT = 4
PENALTY_TIME = 3
PENALTY_BATCH = 6
PENALTY_REPAIRED = 5
# A production date is always behind us, and packs print it near the use-by.
PENALTY_PAST = 6
# Under any date with a day, but over a bare run of digits that may be a
# batch code.
SCORE_MONTH_YEAR = -1
# What live reading needs before it captures: a separated date, so a batch
# code or a half-framed date doesn't fire it.
CONFIDENT_SCORE = 3

_MONTH_REPAIRS = {
    "jn": "jan", "jpn": "jan", "ja n": "jan",
    "10v": "nov", "14ov": "nov", "110v": "nov", "1ov": "nov", "hov": "nov", "nqv": "nov", "n0v": "nov",
    "fe b": "feb", "f eb": "feb", "ap r": "apr", "ju n": "jun", "ju l": "jul",
    "au g": "aug", "se p": "sep", "oc t": "oct", "de c": "dec",
}
_DATE_SHAPE_RE = re.compile(r"(?<![A-Za-z0-9])(?:\d[0-9OIl]{0,7}[./-][0-9OIl]{1,8}|[0-9OIl]{1,8}[./-][0-9OIl]{1,8})(?![A-Za-z0-9])")


def _repaired_line(line: str) -> str:
    repaired = line
    # Longest first, or "110V" would be eaten as a "1" and a "10V".
    for wrong, right in sorted(_MONTH_REPAIRS.items(), key=lambda item: -len(item[0])):
        repaired = re.sub(rf"(?<=\d)\s*{re.escape(wrong)}(?=\s*(?:\d|$|:))", right, repaired, flags=re.IGNORECASE)
    repaired = re.sub(r"(\b(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\s+\d{2})(\d{2}:\d{2})\b", r"\1 \2", repaired, flags=re.I)
    return _DATE_SHAPE_RE.sub(lambda m: m.group().translate(str.maketrans({"O": "0", "o": "0", "I": "1", "l": "1"})), repaired)


def _line_dates(line: str, *, lenient: bool = False, repair_old_year: bool = False) -> list[tuple[date, bool]]:
    found = []
    variants = [(line, False)]
    repaired = _repaired_line(line)
    if repaired != line:
        variants.append((repaired, True))
    for value, was_repaired in variants:
        parsed = _read_date(value)
        if parsed and _is_recent_or_future(parsed):
            found.append((parsed, was_repaired))
        patterns = [_ISO_RE, _TEXT_DATE_RE, _DMY_RE, _SPACED_DATE_RE, _COMPACT_DATE_RE]
        # "28 SEP 26" must not also count as a yearless "28 SEP", which would
        # roll an expired date forward a year.
        if not _TEXT_DATE_RE.search(value):
            patterns.append(_TEXT_DATE_NO_YEAR_RE)
        for pattern in patterns:
            for match in pattern.finditer(value):
                parsed = _read_date(match.group())
                if parsed and _is_recent_or_future(parsed):
                    found.append((parsed, was_repaired))
                elif parsed and (repair_old_year or lenient) and _has_printed_year(match.group()):
                    repaired_date = _next_year_if_recent(parsed)
                    if repaired_date:
                        found.append((repaired_date, True))
        # Split a line around obvious separators to discover multiple dates.
        for part in re.split(r"\s{2,}|\s+(?=(?:USE\s*BY|BEST\s*BEFORE|BB|EXP)\b)", value, flags=re.I):
            parsed = _read_date(part)
            if parsed and _is_recent_or_future(parsed):
                found.append((parsed, was_repaired))
    unique = {}
    for parsed, repaired_flag in found:
        if parsed not in unique or unique[parsed] and not repaired_flag:
            unique[parsed] = repaired_flag
    return [(parsed, flag) for parsed, flag in unique.items()]


def _has_printed_year(text: str) -> bool:
    patterns = (_ISO_RE, _DMY_RE, _TEXT_DATE_RE, _SPACED_DATE_RE, _COMPACT_DATE_RE)
    return any(pattern.search(text) for pattern in patterns)


def _next_year_if_recent(parsed: date) -> date | None:
    if (date.today() - parsed).days <= _MAX_PAST_DAYS:
        return None
    try:
        repaired = date(parsed.year + 1, parsed.month, parsed.day)
    except ValueError:
        return None
    if (date.today() - repaired).days <= _NO_YEAR_GRACE_DAYS:
        return repaired
    return None


def _candidate_tier(candidate: dict) -> int:
    """A well-formed date beats any repair, but a repair beats a bare run of
    digits that scored badly and is probably a batch code."""
    if candidate["repaired"]:
        return 1
    return 2 if candidate["score"] >= CONFIDENT_SCORE else 0


def date_candidates(text: str, *, lenient: bool = False) -> list[dict]:
    """Returns ranked, deduplicated date readings with their source line."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    date_match = _DATE_LINE_RE.search(text)
    if date_match and date_match[1].strip().upper() == "UNKNOWN":
        return []
    if date_match:
        answer = date_match[1].strip()
        parsed = _line_dates(answer, lenient=lenient)
        if parsed:
            return [{"date": d.isoformat(), "score": 100, "repaired": r, "line": answer} for d, r in parsed]
        lines = [line for line in lines if not _DATE_LINE_RE.match(line)]
    candidates = []
    for index, line in enumerate(lines):
        labelled_here = bool(_LABEL_RE.search(line))
        label_previous = any(
            _LABEL_RE.search(previous)
            for previous in lines[max(0, index - 2):index]
        )
        reads = _line_dates(line, lenient=lenient, repair_old_year=labelled_here or label_previous)
        if not reads and lenient:
            # Human-assisted mode accepts day/month only and repaired forms.
            reads = _line_dates(_repaired_line(line), lenient=True)
        for parsed, repaired in reads:
            score = SCORE_LABELLED if labelled_here else (SCORE_PREVIOUS_LABEL if label_previous else 0)
            if re.search(r"[./-]|\b(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\b", line, re.I): score += SCORE_SEPARATORS
            if re.search(rf"(?<![:\d])(?:{parsed.year}|{parsed.year % 100:02d})(?![:\d])", line): score += SCORE_PRINTED_YEAR
            if parsed < date.today(): score -= PENALTY_PAST
            if re.search(r"(?<!\d)\d{6}(?!\d)", line) and not (labelled_here or label_previous): score -= PENALTY_COMPACT
            if re.search(r"\b\d{1,2}:\d{2}\b", line) and not re.search(r"\b\d{1,2}\s+[A-Z]{3}\s+\d{1,2}:\d{2}\b", line, re.I): score -= PENALTY_TIME
            if re.search(r"\b(?:L|LOT)\s*\w*\d", line, re.I): score -= PENALTY_BATCH
            if repaired: score -= PENALTY_REPAIRED
            candidates.append({"date": parsed.isoformat(), "score": score, "repaired": repaired, "line": line})
        # A two-digit year needs a label, since "APR 27" could be a day. A
        # four-digit one doesn't, or a tight crop would lose the date.
        has_label = labelled_here or label_previous
        for match in _MONTH_YEAR_TEXT_RE.finditer(line):
            if re.search(r"\d\s*$", line[:match.start()]) or not (has_label or len(match[2]) == 4):
                continue
            parsed = _month_year_date(_MONTHS[match[1].lower()], int(match[2]))
            if parsed and _is_recent_or_future(parsed) and _within_month_year_window(parsed):
                candidates.append({"date": parsed.isoformat(), "score": SCORE_MONTH_YEAR, "repaired": False, "line": line})
        for match in _MONTH_YEAR_NUMERIC_RE.finditer(line):
            year_text = match[3] or match[4]
            if not (has_label or len(year_text) == 4):
                continue
            parsed = _month_year_date(int(match[1]), int(year_text))
            if parsed and _is_recent_or_future(parsed) and _within_month_year_window(parsed):
                candidates.append({"date": parsed.isoformat(), "score": SCORE_MONTH_YEAR, "repaired": False, "line": line})
    best = {}
    for candidate in candidates:
        old = best.get(candidate["date"])
        if old is None or (not candidate["repaired"], candidate["score"]) > (not old["repaired"], old["score"]):
            best[candidate["date"]] = candidate
    if not best and len(lines) == 1 and not _DATE_LINE_RE.match(lines[0]):
        parsed = _parse_date_text(lines[0])
        if parsed:
            # _parse_date_text may have moved a misread year, which is as much
            # a guess as any other repair.
            printed = _read_date(lines[0])
            best[parsed] = {
                "date": parsed,
                "score": 0,
                "repaired": not printed or printed.isoformat() != parsed,
                "line": lines[0],
            }
    return sorted(best.values(), key=lambda c: (_candidate_tier(c), c["score"], c["date"]), reverse=True)


def is_confident(candidate: dict) -> bool:
    return not candidate["repaired"] and candidate["score"] >= CONFIDENT_SCORE


def _normalise_year(year: int) -> int:
    # Two-digit years on packaging are always near-future, never 1900s.
    return 2000 + year if year < 100 else year


def _month_year_date(month: int, year: int) -> date | None:
    year = _normalise_year(year)
    try:
        return date(year, month, calendar.monthrange(year, month)[1])
    except ValueError:
        return None


def _within_month_year_window(parsed: date) -> bool:
    return parsed <= date(date.today().year + _MAX_FUTURE_YEARS, 12, 31)


def _resolve_unknown_year(month: int, day: int) -> int:
    """The year of the nearest occurrence of a day and month that isn't
    long past. Scans forward a few years to also cover 29 February."""
    earliest = date.today() - timedelta(days=_NO_YEAR_GRACE_DAYS)
    for year in range(earliest.year, earliest.year + 9):
        try:
            if date(year, month, day) >= earliest:
                return year
        except ValueError:
            continue  # 29 Feb in a non-leap year, or not a real date at all
    return earliest.year


def _read_unseparated_date(text: str) -> date | None:
    today = date.today()
    earliest = today - timedelta(days=_MAX_PAST_DAYS)
    latest = today + timedelta(days=_MAX_FUTURE_YEARS * 365)
    for pattern in (_SPACED_DATE_RE, _COMPACT_DATE_RE):
        for match in pattern.finditer(text):
            day, month, year = (int(g) for g in match.groups())
            try:
                parsed = date(_normalise_year(year), month, day)
            except ValueError:
                continue
            if earliest <= parsed <= latest:
                return parsed
    return None


def _read_date(text: str) -> date | None:
    match = _ISO_RE.search(text)
    if match:
        try:
            return date(*(int(g) for g in match.groups()))
        except ValueError:
            pass

    match = _TEXT_DATE_RE.search(text)
    if match:
        try:
            return date(_normalise_year(int(match[3])), _MONTHS[match[2].lower()], int(match[1]))
        except ValueError:
            pass

    match = _DMY_RE.search(text)
    if match:
        day, month, year = (int(g) for g in match.groups())
        try:
            return date(_normalise_year(year), month, day)
        except ValueError:
            pass

    parsed = _read_unseparated_date(text)
    if parsed:
        return parsed

    match = _TEXT_DATE_NO_YEAR_RE.search(text)
    if match:
        day, month = int(match[1]), _MONTHS[match[2].lower()]
        try:
            return date(_resolve_unknown_year(month, day), month, day)
        except ValueError:
            pass

    match = _DM_RE.match(text)
    if match:
        day, month = int(match[1]), int(match[2])
        try:
            return date(_resolve_unknown_year(month, day), month, day)
        except ValueError:
            pass

    return None


def _parse_date_text(text: str) -> str | None:
    parsed = _read_date(text.strip())
    if parsed is None:
        return None
    if (date.today() - parsed).days > _MAX_PAST_DAYS:
        try:
            parsed = date(_resolve_unknown_year(parsed.month, parsed.day), parsed.month, parsed.day)
        except ValueError:
            return None
    return parsed.isoformat()


def _is_recent_or_future(parsed: date) -> bool:
    return (date.today() - parsed).days <= _MAX_PAST_DAYS


def parse_transcript(text: str) -> str | None:
    """The use-by date in text read off packaging, as YYYY-MM-DD. Handles a
    bare date, a transcript of every line, or a transcript ending in the
    DATE: line _PROMPT asks for. Takes the latest of several dates, since
    packs that print two show the production date first."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) == 1 and not _DATE_LINE_RE.match(lines[0]):
        parsed = _parse_date_text(lines[0])
        if parsed:
            return parsed
        ranked_single = date_candidates(lines[0])
        return ranked_single[0]["date"] if ranked_single else None

    match = _DATE_LINE_RE.search(text)
    if match:
        answer = match[1].strip()
        if answer.upper() == "UNKNOWN":
            return None
        parsed = _read_date(answer)
        if parsed and _is_recent_or_future(parsed):
            return parsed.isoformat()
        # A DATE: line that isn't one ("DATE: 27") can still follow a line
        # the model copied correctly.
        lines = [line for line in lines if not _DATE_LINE_RE.match(line)]

    # Unlike a single line, a transcript's old dates are usually real print
    # rather than a misread year, so they're dropped and not moved forward.
    ranked = date_candidates("\n".join(lines))
    if ranked:
        return ranked[0]["date"]
    old_dates = [parsed for parsed in map(_read_date, lines) if parsed and _is_recent_or_future(parsed)]
    return max(old_dates).isoformat() if old_dates else None


def _prompt_for(model: str) -> str:
    return _OCR_MODEL_PROMPT if "glm-ocr" in model.lower() else _PROMPT


def configured() -> bool:
    return bool(settings.vision_api_base())


def extract_use_by_date(image_bytes: bytes) -> dict:
    """Returns the top date, ranked candidates, and the model reply."""
    if not configured():
        return {"date": None, "candidates": [], "raw": "Vision model is not configured"}

    try:
        image = Image.open(BytesIO(image_bytes))
        image = ImageOps.exif_transpose(image)
        image.thumbnail((_MAX_DIMENSION, _MAX_DIMENSION))
        buffer = BytesIO()
        image.convert("RGB").save(buffer, format="JPEG", quality=85)
        b64 = base64.b64encode(buffer.getvalue()).decode("ascii")
    except Exception as exc:
        return {"date": None, "candidates": [], "raw": f"Could not read photo: {exc}"}

    body = {
        "model": settings.vision_model(),
        "stream": False,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _prompt_for(settings.vision_model())},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            }
        ],
    }
    headers = {"Content-Type": "application/json"}
    api_key = settings.vision_api_key()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    url = f"{settings.vision_api_base()}/chat/completions"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            payload = json.loads(resp.read())
        reply = payload["choices"][0]["message"]["content"]
    except (urllib.error.URLError, TimeoutError, ConnectionError, KeyError, IndexError, json.JSONDecodeError, ValueError) as exc:
        retryable = isinstance(exc, (TimeoutError, ConnectionError))
        if isinstance(exc, urllib.error.URLError) and not isinstance(exc, urllib.error.HTTPError):
            retryable = isinstance(exc.reason, (TimeoutError, ConnectionError, socket.gaierror, OSError))
        return {"date": None, "candidates": [], "raw": f"Vision request failed: {exc}", "retryable": retryable}

    reply = reply.strip()
    if reply.upper() == "UNKNOWN":
        return {"date": None, "candidates": [], "raw": reply}
    candidates = date_candidates(reply)
    return {"date": candidates[0]["date"] if candidates else None, "candidates": candidates, "raw": reply}
