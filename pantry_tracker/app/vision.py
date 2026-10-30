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
from datetime import date
from io import BytesIO

from PIL import Image, ImageOps

import settings

_TIMEOUT_SECONDS = 60
# A phone photo carries far more detail than a vision model needs to read a
# printed date; downscaling first makes the round trip to the model much
# faster without hurting legibility.
_MAX_DIMENSION = 1024

_PROMPT = (
    "This photo shows a food package. Find the printed use-by, best-before, "
    "or expiry date on it. Reply with ONLY that date in strict YYYY-MM-DD "
    "format (assume the 21st century for a 2-digit year). If the day and "
    "month are printed but no year is printed anywhere near them, reply "
    "with 0000 as the year instead of guessing one, e.g. 0000-10-31. If no "
    "such date is visible or legible, reply with exactly: UNKNOWN"
)

_ISO_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
# DD/MM/YYYY, DD-MM-YYYY, DD.MM.YYYY - the convention almost all UK/EU
# packaging prints dates in, which is what this app is primarily built for.
_DMY_RE = re.compile(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\b")

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
_TEXT_DATE_RE = re.compile(r"\b(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{2,4})\b")
# Last-resort: a day + month name with no year token at all nearby (the
# model ignored the "reply with 0000" instruction and just said e.g.
# "31 Oct" or "31st October"). Only checked after every year-aware pattern
# above has failed to match.
_TEXT_DATE_NO_YEAR_RE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\b")

_UNKNOWN_YEAR = "0000"


def _normalise_year(year: int) -> int:
    # Two-digit years on packaging are always near-future, never 1900s.
    return 2000 + year if year < 100 else year


def _resolve_unknown_year(month: int, day: int) -> int:
    """Picks a year for a day/month with no printed year, favouring the
    nearest occurrence that is today or still to come. Packaging almost
    never actually omits the year - a model reply with none usually means
    it couldn't read it and would otherwise guess, and a guessed year
    landing in the past (e.g. "31 Oct" coming back as 31 Oct 2023) is a
    worse default than assuming an item just bought is not already
    expired. Scans forward a few years to also cover 29 February.
    """
    today = date.today()
    candidates = []
    for year in range(today.year, today.year + 8):
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            continue  # 29 Feb in a non-leap year
    for candidate in candidates:
        if candidate >= today:
            return candidate.year
    return candidates[0].year if candidates else today.year


def _parse_date_text(text: str) -> str | None:
    text = text.strip()

    match = _ISO_RE.search(text)
    if match:
        year_s, month_s, day_s = match.groups()
        month, day = int(month_s), int(day_s)
        year = _resolve_unknown_year(month, day) if year_s == _UNKNOWN_YEAR else int(year_s)
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            pass

    match = _TEXT_DATE_RE.search(text)
    if match:
        day_s, month_name, year_s = match.groups()
        month = _MONTHS.get(month_name.lower()[:3])
        day = int(day_s)
        if month:
            year = _resolve_unknown_year(month, day) if year_s == _UNKNOWN_YEAR else _normalise_year(int(year_s))
            try:
                return date(year, month, day).isoformat()
            except ValueError:
                pass

    match = _DMY_RE.search(text)
    if match:
        day_s, month_s, year_s = match.groups()
        day, month = int(day_s), int(month_s)
        year = _resolve_unknown_year(month, day) if year_s == _UNKNOWN_YEAR else _normalise_year(int(year_s))
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            pass

    match = _TEXT_DATE_NO_YEAR_RE.search(text)
    if match:
        day_s, month_name = match.groups()
        month = _MONTHS.get(month_name.lower()[:3])
        day = int(day_s)
        if month:
            try:
                return date(_resolve_unknown_year(month, day), month, day).isoformat()
            except ValueError:
                pass

    return None


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
