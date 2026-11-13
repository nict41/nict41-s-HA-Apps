"""Font loading.

The image installs font-inter and font-dejavu via apk, plus the Material
Design Icons webfont (downloaded at build time, codepoints in
/app/mdi/codepoints.json) and a set of bundled display fonts in /app/fonts
(downloaded at build time from google/fonts, see the Dockerfile). In tests
none of these may exist - everything falls back to Pillow's built-in default
font, and icon rendering is skipped.

`load_font(size, bold, family)` picks a font: family None/"default" is the
usual UI font, a bundled family id ("bebas-neue", ...) is one of /app/fonts,
and "file:<name>" is a user font in <screens file folder>/fonts/<name> (the
folder is set with set_user_dir, where the controller sets images_dir).
Unknown families and missing files log one warning per name and fall back
to the default font - they never raise.
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

# Bundled fonts downloaded at image build time into /app/fonts (see Dockerfile).
# id -> (file name in /app/fonts, display name for the editor). "default" is the
# regular UI font (Inter/DejaVu from apk), not a file in /app/fonts.
FONTS_DIR = Path(os.environ.get("TD_FONTS_DIR", "/app/fonts"))
FAMILIES = {
    "default": (None, "Default"),
    "bebas-neue": ("BebasNeue-Regular.ttf", "Bebas Neue"),
    "orbitron": ("Orbitron[wght].ttf", "Orbitron"),
    "share-tech-mono": ("ShareTechMono-Regular.ttf", "Share Tech Mono"),
    "oswald": ("Oswald[wght].ttf", "Oswald"),
    "jetbrains-mono": ("JetBrainsMono[wght].ttf", "JetBrains Mono"),
    "nunito": ("Nunito[wght].ttf", "Nunito"),
    "playfair": ("PlayfairDisplay[wght].ttf", "Playfair Display"),
    "press-start": ("PressStart2P-Regular.ttf", "Press Start 2P"),
    "roboto-condensed": ("RobotoCondensed[wght].ttf", "Roboto Condensed"),
    "audiowide": ("Audiowide-Regular.ttf", "Audiowide"),
}
FONT_EXTS = (".ttf", ".otf")

# Folder of user-uploaded fonts ("file:<name>"), set by the controller/editor
# next to where images_dir is set.
_USER_DIR: str | None = None
# family/file name -> warned once about (missing file / unknown family)
_WARNED: set[str] = set()


def set_user_dir(path: str | None) -> None:
    """The folder user fonts live in (<screens file folder>/fonts)."""
    global _USER_DIR
    _USER_DIR = path
    _load.cache_clear()


def user_dir() -> str | None:
    return _USER_DIR


def family_file(family: str) -> str | None:
    """The /app/fonts file name of a bundled family id, or None."""
    entry = FAMILIES.get(family)
    return entry[0] if entry else None


def family_display_name(family: str) -> str:
    entry = FAMILIES.get(family)
    return entry[1] if entry else family


def bundled_families() -> list[dict]:
    """[{"id", "name"}] for the editor's font picker (default first)."""
    return [{"id": fid, "name": name} for fid, (_, name) in FAMILIES.items()]


def safe_font_name(name: str) -> str | None:
    """The name if it is a plain font file name in the user fonts folder, else None."""
    if (not name or name != os.path.basename(name) or name.startswith(".")
            or not name.lower().endswith(FONT_EXTS)
            or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._- " for c in name)):
        return None
    return name


def user_font_path(name: str) -> str | None:
    """The full path of a user font "file:<name>", or None when the name is unsafe."""
    safe = safe_font_name(name)
    if safe is None or not _USER_DIR:
        return None
    return os.path.join(_USER_DIR, safe)


def _warn_once(key: str, msg: str) -> None:
    if key not in _WARNED:
        _WARNED.add(key)
        _LOG.warning("%s - using the default font", msg)


def _family_path(family: str) -> str | None:
    """The file path for a family id: bundled file, or a user font for "file:<name>"."""
    if family.startswith("file:"):
        path = user_font_path(family[len("file:"):])
        if path is None:
            _warn_once("file:" + family[5:], f"bad user font name {family[5:]!r}")
            return None
        if not os.path.isfile(path):
            _warn_once("file:" + family[5:], f"user font {family[5:]!r} not found in {_USER_DIR}")
            return None
        return path
    fname = family_file(family)
    if fname is None:
        _warn_once("family:" + family, f"unknown font family {family!r}")
        return None
    path = str(FONTS_DIR / fname)
    if not os.path.isfile(path):
        _warn_once("family:" + family, f"bundled font {fname!r} not found in {FONTS_DIR}")
        return None
    return path


def _has_wght_axis(font: ImageFont.FreeTypeFont) -> bool:
    try:
        return any(str(a.get("tag", "")).strip().lower() == "wght" for a in font.get_variation_axes())
    except Exception:
        return False


def _apply_weight(font: ImageFont.FreeTypeFont, bold: bool) -> ImageFont.FreeTypeFont:
    """Variable fonts get wght 700 (bold) / 400 (regular); static fonts ignore it."""
    if not _has_wght_axis(font):
        return font
    try:
        axes = font.get_variation_axes()
        values = [700.0 if (str(a.get("tag", "")).strip().lower() == "wght") else float(a.get("default", 0))
                  for a in axes]
        if not bold:
            values = [400.0 if (str(a.get("tag", "")).strip().lower() == "wght") else v
                      for a, v in zip(axes, values)]
        font.set_variation_by_axes(values)
    except Exception:
        try:
            font.set_variation_by_name("Bold" if bold else "Regular")
        except Exception:
            pass
    return font


@lru_cache(maxsize=256)
def _load(path: str, size: int, bold: bool) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """The font object for (path, size, bold); variable weights are applied per call
    on a fresh instance, so the cache key stays (path, size, bold)."""
    font = ImageFont.truetype(path, size)
    return _apply_weight(font, bold)


@lru_cache(maxsize=64)
def _default_font(size: int, bold: bool) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Today's default font (Inter/DejaVu, bold variant when bold), cached."""
    path = _bold_path() if bold else _regular_path()
    if path:
        try:
            return _load(path, size, bold)
        except OSError:
            _LOG.warning("Could not load font %s - using Pillow default", path)
    return ImageFont.load_default(size=size) if size else ImageFont.load_default()


@lru_cache(maxsize=64)
def load_font(size: int, bold: bool = False, family: str | None = None) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """A UI font at `size`, falling back to Pillow's default font.

    family None/"default" is today's behaviour (Inter/DejaVu, bold variant when
    bold); a bundled family id or "file:<name>" picks that font instead. A
    missing file or unknown family logs one warning and falls back to the
    default font - never raises."""
    if family and family != "default":
        path = _family_path(family)
        if path is not None:
            try:
                return _load(path, size, bold)
            except OSError:
                _warn_once("load:" + path, f"could not load font {path}")
    return _default_font(size, bold)

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
