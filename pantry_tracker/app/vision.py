"""Extracts a use-by/best-before date from a photo using a self-hosted
vision-language model, via an Open WebUI / Ollama OpenAI-compatible
`/chat/completions` endpoint (e.g. qwen2.5vl running locally). The photo
only ever goes to the configured endpoint on the user's own network - never
to a third-party vision API.

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

_TIMEOUT_SECONDS = 60
# Photos arrive already cropped to a date, so most of their pixels are the
# print itself - this only reins in an uncropped photo from the file picker.
_MAX_DIMENSION = 2048

# Asks for a copy of the print rather than a formatted date. Told to output
# YYYY-MM-DD, the model invents a year for packaging that doesn't print one
# (2021 and 2023 in real scans), so working out the year is left to
# _resolve_unknown_year below.
_PROMPT = (
    "This is a photo of a printed use-by or best-before date on food "
    "packaging. Copy that date exactly as it is printed, character for "
    "character. Do not add a year if none is printed, and do not convert "
    "the format. Ignore times, batch codes and lot numbers. Reply with only "
    "the copied date. If no date is legible, reply with exactly: UNKNOWN"
)

_ISO_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
# DD/MM/YYYY, DD-MM-YYYY, DD.MM.YYYY - the convention almost all UK/EU
# packaging prints dates in, which is what this app is primarily built for.
_DMY_RE = re.compile(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\b")
_DM_RE = re.compile(r"\b(\d{1,2})[./-](\d{1,2})\b")

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
_MONTH_PATTERN = "(" + "|".join(names[0] for names in _MONTH_NAMES) + ")[a-z]*"
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

    match = _TEXT_DATE_NO_YEAR_RE.search(text)
    if match:
        day, month = int(match[1]), _MONTHS[match[2].lower()]
        try:
            return date(_resolve_unknown_year(month, day), month, day)
        except ValueError:
            pass

    match = _DM_RE.search(text)
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
                    {"type": "text", "text": _PROMPT},
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
    return {"date": _parse_date_text(reply), "raw": reply}
