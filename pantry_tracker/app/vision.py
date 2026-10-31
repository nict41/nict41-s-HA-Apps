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
import json
import re
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


def _normalise_year(year: int) -> int:
    # Two-digit years on packaging are always near-future, never 1900s.
    return 2000 + year if year < 100 else year


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
        return _parse_date_text(lines[0])

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
    candidates = [parsed for parsed in map(_read_date, lines) if parsed and _is_recent_or_future(parsed)]
    return max(candidates).isoformat() if candidates else None


def _prompt_for(model: str) -> str:
    return _OCR_MODEL_PROMPT if "glm-ocr" in model.lower() else _PROMPT


def configured() -> bool:
    return bool(settings.vision_api_base())


def extract_use_by_date(image_bytes: bytes) -> dict:
    """Returns {"date": "YYYY-MM-DD" | None, "raw": "<model reply or error>"}."""
    if not configured():
        return {"date": None, "raw": "Vision model is not configured"}

    try:
        image = Image.open(BytesIO(image_bytes))
        image = ImageOps.exif_transpose(image)
        image.thumbnail((_MAX_DIMENSION, _MAX_DIMENSION))
        buffer = BytesIO()
        image.convert("RGB").save(buffer, format="JPEG", quality=85)
        b64 = base64.b64encode(buffer.getvalue()).decode("ascii")
    except Exception as exc:
        return {"date": None, "raw": f"Could not read photo: {exc}"}

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
    except (urllib.error.URLError, TimeoutError, KeyError, IndexError, json.JSONDecodeError, ValueError) as exc:
        return {"date": None, "raw": f"Vision request failed: {exc}"}

    reply = reply.strip()
    if reply.upper() == "UNKNOWN":
        return {"date": None, "raw": reply}
    return {"date": parse_transcript(reply), "raw": reply}
