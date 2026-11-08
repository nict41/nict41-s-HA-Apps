"""Font loading.

The image installs font-inter and font-dejavu via apk, plus the Material
Design Icons webfont (downloaded at build time, codepoints in
/app/mdi/codepoints.json). In tests none of these may exist - everything falls
back to Pillow's built-in default font, and icon rendering is skipped.
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from pathlib import Path

from PIL import ImageFont

_LOG = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent
MDI_DIR = APP_DIR / "mdi"
MDI_FONT = MDI_DIR / "materialdesignicons-webfont.ttf"
MDI_CODEPOINTS = MDI_DIR / "codepoints.json"

# Alpine package font paths (font-inter, font-dejavu).
_FONT_CANDIDATES = (
    "/usr/share/fonts/inter/Inter-Regular.ttf",
    "/usr/share/fonts/inter/Inter[wght].ttf",
    "/usr/share/fonts/truetype/inter/Inter-Regular.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
)
_FONT_BOLD_CANDIDATES = (
    "/usr/share/fonts/inter/Inter-Bold.ttf",
    "/usr/share/fonts/inter/Inter-SemiBold.ttf",
    "/usr/share/fonts/truetype/inter/Inter-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
)


def _first_existing(paths) -> str | None:
    for p in paths:
        if os.path.exists(p):
            return p
    return None


def _search(patterns) -> str | None:
    """First font under /usr/share/fonts matching one of the glob patterns, in order. The exact
    file names differ between package versions, so the fixed paths above aren't relied on."""
    root = Path("/usr/share/fonts")
    if not root.is_dir():
        return None
    for pattern in patterns:
        for p in sorted(root.rglob(pattern)):
            if p.suffix.lower() in (".ttf", ".otf", ".ttc"):
                return str(p)
    return None


@lru_cache(maxsize=1)
def _regular_path() -> str | None:
    return _first_existing(_FONT_CANDIDATES) or _search(
        ("Inter-Regular.*", "InterVariable.*", "Inter.*", "Inter*.*", "DejaVuSans.*"))


@lru_cache(maxsize=1)
def _bold_path() -> str | None:
    return _first_existing(_FONT_BOLD_CANDIDATES) or _search(
        ("Inter-Bold.*", "Inter-SemiBold.*", "InterVariable.*", "Inter.*", "DejaVuSans-Bold.*")) or _regular_path()


@lru_cache(maxsize=64)
def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """A UI font at `size`, falling back to Pillow's default font."""
    path = _bold_path() if bold else _regular_path()
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            _LOG.warning("Could not load font %s - using Pillow default", path)
    return ImageFont.load_default(size=size) if size else ImageFont.load_default()


@lru_cache(maxsize=1)
def mdi_available() -> bool:
    return MDI_FONT.exists() and MDI_CODEPOINTS.exists()


@lru_cache(maxsize=1)
def _codepoints() -> dict:
    try:
        with open(MDI_CODEPOINTS, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def mdi_codepoint(name: str) -> int | None:
    """Codepoint for an mdi icon name (without the 'mdi:' prefix), or None."""
    return _codepoints().get(name)


@lru_cache(maxsize=8)
def load_mdi_font(size: int) -> ImageFont.FreeTypeFont | None:
    if not mdi_available():
        return None
    try:
        return ImageFont.truetype(str(MDI_FONT), size)
    except OSError:
        return None
