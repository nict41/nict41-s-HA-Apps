"""Rendering screens to an 810x240 strip image (six 135x240 tubes).

Every widget is a small function drawing into a 135x240 Pillow image. String
fields may be Home Assistant templates; all templates of a screen are rendered
concurrently before drawing. A widget whose template fails (or that fails to
draw) shows "!" and the error is logged once per change, not every refresh.
"""

from __future__ import annotations

import asyncio
import io
import os
import logging
import math
import random
from typing import Any, Awaitable, Callable

from PIL import Image, ImageDraw, ImageFont

import fonts
from rgb565 import STRIP_H, STRIP_W, TUBE_H, TUBE_W
from screens import Screen

_LOG = logging.getLogger(__name__)

CLAUDE_ORANGE = (0xD9, 0x77, 0x57)
DEFAULT_COLOR = (255, 255, 255)
TRACK_COLOR = (60, 60, 60)

TemplateRenderer = Callable[[str], Awaitable[str]]


class RenderContext:
    """Everything a widget needs: resolved values are passed per widget."""

    def __init__(self, renderer: TemplateRenderer, session=None, ha=None) -> None:
        self.renderer = renderer
        self.session = session
        self.ha = ha
        # Folder of uploaded pictures for "file:<name>" sources (set by the controller)
        self.images_dir: str | None = None
        # widget key -> last error message, so an error is logged once per change
        self.last_errors: dict[str, str] = {}

    def note_error(self, key: str, msg: str) -> None:
        if self.last_errors.get(key) != msg:
            _LOG.error("Widget %s: %s", key, msg)
            self.last_errors[key] = msg

    def clear_error(self, key: str) -> None:
        self.last_errors.pop(key, None)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def parse_color(value: Any, default=(255, 255, 255)) -> tuple[int, int, int]:
    """'#RRGGBB' (or '#RGB') -> RGB tuple; anything else -> default."""
    if not isinstance(value, str):
        return default
    s = value.strip()
    if s.startswith("#"):
        s = s[1:]
    try:
        if len(s) == 3:
            return tuple(int(c * 2, 16) for c in s)  # type: ignore[return-value]
        if len(s) == 6:
            return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except ValueError:
        pass
    return default


def _text_size(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def _fit_font(draw: ImageDraw.ImageDraw, text: str, max_w: int, max_h: int,
              start: int, bold: bool = False, min_size: int = 8) -> ImageFont.ImageFont:
    """Largest font size <= start whose text fits max_w x max_h."""
    size = max(start, min_size)
    while size > min_size:
        font = fonts.load_font(size, bold=bold)
        w, h = _text_size(draw, text, font)
        if w <= max_w and h <= max_h:
            return font
        size -= 1
    return fonts.load_font(min_size, bold=bold)


def _draw_centered(draw: ImageDraw.ImageDraw, cx: int, cy: int, text: str,
                   font, fill) -> None:
    # Centre the ink, not the text origin: fonts leave space above the glyphs (bbox top), which
    # at large sizes pushes text down and off the tube
    x0, y0, x1, y1 = draw.textbbox((0, 0), text, font=font)
    draw.text((cx - (x0 + x1) / 2, cy - (y0 + y1) / 2), text, font=font, fill=fill)


def _draw_error(img: Image.Image, msg: str = "!") -> Image.Image:
    draw = ImageDraw.Draw(img)
    font = fonts.load_font(48, bold=True)
    _draw_centered(draw, TUBE_W // 2, TUBE_H // 2, msg, font, (255, 80, 80))
    return img


def _parse_number(value: Any) -> float | None:
    try:
        return float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# template collection / resolution
# ---------------------------------------------------------------------------

_WIDGET_TEMPLATE_FIELDS = {
    "text": ("text", "color"),
    "big_number": ("value", "unit", "label", "color"),
    "ring": ("value", "max", "label", "unit", "color"),
    "bar": ("value", "max", "label", "unit", "color"),
    "icon": ("icon", "text", "color"),
    "image": ("source",),
    "sparkline": ("entity", "color", "unit"),
    "blank": (),
}


def collect_templates(screen: Screen) -> list[str]:
    """All template strings in a screen (deduplicated, order preserved)."""
    found: list[str] = []
    seen: set[str] = set()

    def add(s: Any) -> None:
        if isinstance(s, str) and "{{" in s and s not in seen:
            seen.add(s)
            found.append(s)

    add(screen.when)
    add(screen.background)
    if screen.strip:
        add(screen.strip.get("source"))
    for w in screen.tubes:
        for field in _WIDGET_TEMPLATE_FIELDS.get(w.get("type"), ()):
            add(w.get(field))
    return found


async def resolve_templates(screen: Screen, ctx: RenderContext) -> dict[str, str]:
    """Render every template of the screen concurrently -> template -> result."""
    templates = collect_templates(screen)
    if not templates:
        return {}
    results = await asyncio.gather(
        *(ctx.renderer(t) for t in templates), return_exceptions=True
    )
    return {t: (r if isinstance(r, str) else f"__error__{r}") for t, r in zip(templates, results)}


def _sub(value: Any, resolved: dict[str, str]) -> Any:
    """Substitute a fully-rendered template string; errors become None."""
    if isinstance(value, str) and value in resolved:
        r = resolved[value]
        return None if r.startswith("__error__") else r
    return value


def _resolved_widget(w: dict, resolved: dict[str, str]) -> dict:
    out = dict(w)
    for field in _WIDGET_TEMPLATE_FIELDS.get(w.get("type"), ()):
        if field in out:
            out[field] = _sub(out[field], resolved)
    return out


# ---------------------------------------------------------------------------
# widgets - each draws into a fresh 135x240 image
# ---------------------------------------------------------------------------

def draw_text(w: dict, ctx: RenderContext, bg) -> Image.Image:
    img = Image.new("RGB", (TUBE_W, TUBE_H), bg)
    draw = ImageDraw.Draw(img)
    text = str(w.get("text") or "")
    color = parse_color(w.get("color"), DEFAULT_COLOR)
    align = str(w.get("align", "center")).lower()
    bold = str(w.get("weight", "")).lower() == "bold"
    size = w.get("size", "auto")
    pad = 8
    lines = text.split("\n")
    if size == "auto" or size is None:
        # fit the whole block
        start = 120
        while start > 8:
            font = fonts.load_font(start, bold=bold)
            widths = [_text_size(draw, ln, font)[0] for ln in lines]
            line_h = _text_size(draw, "Ag", font)[1] + 4
            if max(widths, default=0) <= TUBE_W - 2 * pad and line_h * len(lines) <= TUBE_H - 2 * pad:
                break
            start -= 2
        font = fonts.load_font(max(start, 8), bold=bold)
    else:
        font = fonts.load_font(int(size), bold=bold)
    line_h = _text_size(draw, "Ag", font)[1] + 4
    total_h = line_h * len(lines)
    y = (TUBE_H - total_h) // 2
    for ln in lines:
        tw, _ = _text_size(draw, ln, font)
        if align == "left":
            x = pad
        elif align == "right":
            x = TUBE_W - pad - tw
        else:
            x = (TUBE_W - tw) // 2
        draw.text((x, y), ln, font=font, fill=color)
        y += line_h
    return img


def draw_big_number(w: dict, ctx: RenderContext, bg) -> Image.Image:
    img = Image.new("RGB", (TUBE_W, TUBE_H), bg)
    draw = ImageDraw.Draw(img)
    value = str(w.get("value") or "")
    unit = str(w.get("unit") or "")
    label = str(w.get("label") or "")
    color = parse_color(w.get("color"), DEFAULT_COLOR)
    if unit and w.get("unit_newline"):
        # The value alone gets the full width; the unit goes on its own line below it
        font = _fit_font(draw, value, TUBE_W - 8, TUBE_H - 110, start=140, bold=True)
        _draw_centered(draw, TUBE_W // 2, TUBE_H // 2 - 34, value, font, color)
        ufont = _fit_font(draw, unit, TUBE_W - 12, 40, start=36, bold=True)
        _draw_centered(draw, TUBE_W // 2, TUBE_H // 2 + 46, unit, ufont, color)
    elif not unit and not label:
        # Just a value (e.g. one digit of a time): as big as the tube allows
        font = _fit_font(draw, value, TUBE_W - 6, TUBE_H - 24, start=240, bold=True)
        _draw_centered(draw, TUBE_W // 2, TUBE_H // 2, value, font, color)
    else:
        main = value + unit
        font = _fit_font(draw, main, TUBE_W - 12, TUBE_H - 70, start=110, bold=True)
        _draw_centered(draw, TUBE_W // 2, TUBE_H // 2 - 14, main, font, color)
    if label:
        lfont = fonts.load_font(18)
        _draw_centered(draw, TUBE_W // 2, TUBE_H - 34, label, lfont, color)
    return img


def _gauge_fraction(w: dict) -> float:
    value = _parse_number(w.get("value"))
    mx = _parse_number(w.get("max")) or 100.0
    if value is None or mx <= 0:
        return 0.0
    return max(0.0, min(1.0, value / mx))


def draw_ring(w: dict, ctx: RenderContext, bg) -> Image.Image:
    img = Image.new("RGB", (TUBE_W, TUBE_H), bg)
    draw = ImageDraw.Draw(img)
    color = parse_color(w.get("color"), CLAUDE_ORANGE)
    frac = _gauge_fraction(w)
    cx, cy, r = TUBE_W // 2, TUBE_H // 2 - 16, 52
    width = 12
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=TRACK_COLOR, width=width)
    if frac > 0:
        draw.arc([cx - r, cy - r, cx + r, cy + r], start=-90, end=-90 + 360 * frac,
                 fill=color, width=width)
    value = str(w.get("value") or "")
    unit = str(w.get("unit") or "")
    if unit and w.get("unit_newline"):
        vfont = _fit_font(draw, value, 2 * r - 22, 44, start=48, bold=True)
        _draw_centered(draw, cx, cy - 8, value, vfont, DEFAULT_COLOR)
        ufont = _fit_font(draw, unit, 2 * r - 40, 20, start=20, bold=True)
        _draw_centered(draw, cx, cy + 24, unit, ufont, DEFAULT_COLOR)
    else:
        vfont = _fit_font(draw, value + unit, 2 * r - 8, 40, start=34, bold=True)
        _draw_centered(draw, cx, cy, value + unit, vfont, DEFAULT_COLOR)
    label = str(w.get("label") or "")
    if label:
        lfont = fonts.load_font(16)
        _draw_centered(draw, cx, cy + r + 18, label, lfont, DEFAULT_COLOR)
    return img


def draw_bar(w: dict, ctx: RenderContext, bg) -> Image.Image:
    img = Image.new("RGB", (TUBE_W, TUBE_H), bg)
    draw = ImageDraw.Draw(img)
    color = parse_color(w.get("color"), CLAUDE_ORANGE)
    frac = _gauge_fraction(w)
    x0, x1 = 30, TUBE_W - 30
    y0, y1 = 24, TUBE_H - 56
    draw.rectangle([x0, y0, x1, y1], outline=TRACK_COLOR, width=6)
    fill_h = int((y1 - y0) * frac)
    if fill_h > 0:
        draw.rectangle([x0, y1 - fill_h, x1, y1], fill=color)
    value = str(w.get("value") or "") + str(w.get("unit") or "")
    vfont = _fit_font(draw, value, TUBE_W - 16, 40, start=30, bold=True)
    _draw_centered(draw, TUBE_W // 2, (y0 + y1) // 2, value, vfont, DEFAULT_COLOR)
    label = str(w.get("label") or "")
    if label:
        lfont = fonts.load_font(16)
        _draw_centered(draw, TUBE_W // 2, TUBE_H - 30, label, lfont, DEFAULT_COLOR)
    return img


def draw_icon(w: dict, ctx: RenderContext, bg) -> Image.Image:
    img = Image.new("RGB", (TUBE_W, TUBE_H), bg)
    draw = ImageDraw.Draw(img)
    color = parse_color(w.get("color"), CLAUDE_ORANGE)
    icon = str(w.get("icon") or "")
    name = icon[4:] if icon.startswith("mdi:") else icon
    text = str(w.get("text") or "")
    # With text below, the icon sits in the upper part; on its own it is centred and a bit bigger
    cy = 88 if text else TUBE_H // 2
    cp = fonts.mdi_codepoint(name)
    mdi_font = fonts.load_mdi_font(96 if text else 120)
    if cp is not None and mdi_font is not None:
        _draw_centered(draw, TUBE_W // 2, cy, chr(cp), mdi_font, color)
    else:
        # no MDI font (e.g. tests): draw a placeholder box
        draw.rectangle([TUBE_W // 2 - 40, cy - 40, TUBE_W // 2 + 40, cy + 40], outline=color, width=6)
    if text:
        tfont = _fit_font(draw, text, TUBE_W - 12, 60, start=24)
        _draw_centered(draw, TUBE_W // 2, 170, text, tfont, DEFAULT_COLOR)
    return img


def draw_builtin_claude(bg) -> Image.Image:
    """Procedural orange starburst, drawn at 4x and downscaled (anti-aliased)."""
    scale = 4
    img = Image.new("RGB", (TUBE_W * scale, TUBE_H * scale), bg)
    draw = ImageDraw.Draw(img)
    cx, cy = TUBE_W * scale // 2, TUBE_H * scale // 2
    rng = random.Random(20260214)  # fixed seed: the logo is stable
    n_rays = rng.randint(11, 12)
    base_angle = rng.uniform(0, 2 * math.pi)
    radius = 50 * scale  # ~100 px across at 1x
    for i in range(n_rays):
        angle = base_angle + (2 * math.pi * i / n_rays) + rng.uniform(-0.12, 0.12)
        length = radius * rng.uniform(0.72, 1.05)
        base_w = rng.uniform(7, 13) * scale
        dx, dy = math.cos(angle), math.sin(angle)
        px, py = -dy, dx  # perpendicular
        tip = (cx + dx * length, cy + dy * length)
        left = (cx + px * base_w / 2, cy + py * base_w / 2)
        right = (cx - px * base_w / 2, cy - py * base_w / 2)
        draw.polygon([left, right, tip], fill=CLAUDE_ORANGE)
    return img.resize((TUBE_W, TUBE_H), Image.LANCZOS)


async def _fetch_image(source: str, ctx: RenderContext) -> Image.Image:
    """Resolve an image source to a PIL image."""
    if source == "builtin:claude":
        return draw_builtin_claude((0, 0, 0))
    if source.startswith("camera:"):
        if ctx.ha is None:
            raise ValueError("no HA client for camera source")
        data = await ctx.ha.camera_image(source[len("camera:"):])
        return Image.open(io.BytesIO(data)).convert("RGB")
    if source.startswith("entity_picture:"):
        if ctx.ha is None:
            raise ValueError("no HA client for entity_picture source")
        data = await ctx.ha.entity_picture(source[len("entity_picture:"):])
        if data is None:
            raise ValueError("entity has no entity_picture")
        return Image.open(io.BytesIO(data)).convert("RGB")
    if source.startswith("file:"):
        path = image_path(ctx.images_dir, source[len("file:"):])
        with Image.open(path) as img:
            img.seek(0)     # first frame of an animated GIF
            return img.convert("RGBA")
    if source.startswith("http://") or source.startswith("https://"):
        if ctx.session is None:
            raise ValueError("no HTTP session for URL source")
        async with ctx.session.get(source, timeout=aiohttp_timeout()) as resp:
            if resp.status >= 400:
                raise ValueError(f"image GET {source} -> {resp.status}")
            data = await resp.read()
        return Image.open(io.BytesIO(data)).convert("RGB")
    raise ValueError(f"unknown image source {source!r}")


IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")


def _on_background(src: Image.Image, bg) -> Image.Image:
    """Transparent parts of an uploaded picture show the screen's background."""
    if src.mode != "RGBA":
        return src
    base = Image.new("RGBA", src.size, tuple(bg) + (255,))
    return Image.alpha_composite(base, src).convert("RGB")


def safe_image_name(name: str) -> str | None:
    """The name if it is a plain picture file name in the images folder, else None."""
    if (not name or name != os.path.basename(name) or name.startswith(".")
            or not name.lower().endswith(IMAGE_EXTS)
            or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._- " for c in name)):
        return None
    return name


def image_path(images_dir: str | None, name: str) -> str:
    if not images_dir:
        raise ValueError("no images folder")
    safe = safe_image_name(name)
    if safe is None:
        raise ValueError(f"bad image name {name!r}")
    path = os.path.join(images_dir, safe)
    if not os.path.isfile(path):
        raise ValueError(f"image {name!r} not found in {images_dir}")
    return path


def aiohttp_timeout():
    import aiohttp
    return aiohttp.ClientTimeout(total=30)


def _fit_image(src: Image.Image, w: int, h: int, fit: str) -> Image.Image:
    """cover: fill and crop; contain: letterbox."""
    sw, sh = src.size
    if fit == "contain":
        scale = min(w / sw, h / sh)
    else:
        scale = max(w / sw, h / sh)
    nw, nh = max(1, round(sw * scale)), max(1, round(sh * scale))
    src = src.resize((nw, nh), Image.LANCZOS)
    left = (nw - w) // 2
    top = (nh - h) // 2
    return src.crop((left, top, left + w, top + h))


async def draw_image(w: dict, ctx: RenderContext, bg) -> Image.Image:
    source = str(w.get("source") or "")
    fit = str(w.get("fit", "cover"))
    src = await _fetch_image(source, ctx)
    if source == "builtin:claude":
        # starburst is drawn on the screen background
        base = Image.new("RGB", (TUBE_W, TUBE_H), bg)
        base.paste(draw_builtin_claude(bg), (0, 0))
        return base
    return _fit_image(_on_background(src, bg), TUBE_W, TUBE_H, fit)


async def draw_sparkline(w: dict, ctx: RenderContext, bg) -> Image.Image:
    img = Image.new("RGB", (TUBE_W, TUBE_H), bg)
    draw = ImageDraw.Draw(img)
    color = parse_color(w.get("color"), CLAUDE_ORANGE)
    entity = str(w.get("entity") or "")
    hours = float(w.get("hours") or 24)
    if ctx.ha is None:
        raise ValueError("no HA client for sparkline")
    states = await ctx.ha.history(entity, hours)
    values: list[float] = []
    for s in states:
        v = _parse_number(s.get("state"))
        if v is not None:
            values.append(v)
    pad = 10
    top, bottom = 34, TUBE_H - 34
    if values:
        lo, hi = min(values), max(values)
        if hi - lo < 1e-9:
            hi = lo + 1.0
        pts = []
        n = len(values)
        for i, v in enumerate(values):
            x = pad + (TUBE_W - 2 * pad) * (i / max(n - 1, 1))
            y = bottom - (bottom - top) * (v - lo) / (hi - lo)
            pts.append((x, y))
        draw.line(pts, fill=color, width=3, joint="curve")
        # latest value marker + text
        draw.ellipse([pts[-1][0] - 4, pts[-1][1] - 4, pts[-1][0] + 4, pts[-1][1] + 4],
                     fill=color)
        latest = f"{values[-1]:g}" + str(w.get("unit") or "")
        vfont = fonts.load_font(20, bold=True)
        _draw_centered(draw, TUBE_W // 2, 20, latest, vfont, DEFAULT_COLOR)
    else:
        _draw_centered(draw, TUBE_W // 2, (top + bottom) // 2, "no data",
                       fonts.load_font(16), DEFAULT_COLOR)
    return img


def draw_blank(w: dict, ctx: RenderContext, bg) -> Image.Image:
    return Image.new("RGB", (TUBE_W, TUBE_H), bg)


_WIDGET_DRAW = {
    "text": draw_text,
    "big_number": draw_big_number,
    "ring": draw_ring,
    "bar": draw_bar,
    "icon": draw_icon,
    "blank": draw_blank,
}
_WIDGET_DRAW_ASYNC = {
    "image": draw_image,
    "sparkline": draw_sparkline,
}


# ---------------------------------------------------------------------------
# screen rendering
# ---------------------------------------------------------------------------

async def render_screen(screen: Screen, ctx: RenderContext) -> Image.Image:
    """Render a screen to an 810x240 RGB strip image."""
    resolved = await resolve_templates(screen, ctx)
    bg = parse_color(_sub(screen.background, resolved), (0, 0, 0))

    strip = Image.new("RGB", (STRIP_W, STRIP_H), bg)

    if screen.strip is not None:
        w = dict(screen.strip)
        source = _sub(w.get("source"), resolved)
        if source is None:
            ctx.note_error(f"{screen.name}:strip", "template failed")
            return strip
        try:
            src = await _fetch_image(str(source), ctx)
            strip = _fit_image(_on_background(src, bg), STRIP_W, STRIP_H, str(w.get("fit", "cover")))
        except Exception as err:
            ctx.note_error(f"{screen.name}:strip", str(err))
            strip = Image.new("RGB", (STRIP_W, STRIP_H), bg)
            _draw_error(strip)
        return strip

    for i, raw_w in enumerate(screen.tubes):
        key = f"{screen.name}:tube{i}"
        w = _resolved_widget(raw_w, resolved)
        tile = None
        try:
            failed = [f for f in _WIDGET_TEMPLATE_FIELDS.get(raw_w.get("type"), ())
                      if isinstance(raw_w.get(f), str) and resolved.get(raw_w[f], "").startswith("__error__")]
            if failed:
                # Show that the data is broken rather than quietly drawing an empty tube
                raise ValueError(f"template failed for {', '.join(failed)}: "
                                 + resolved[raw_w[failed[0]]][len("__error__"):])
            wtype = w.get("type")
            if wtype in _WIDGET_DRAW:
                tile = _WIDGET_DRAW[wtype](w, ctx, bg)
            elif wtype in _WIDGET_DRAW_ASYNC:
                tile = await _WIDGET_DRAW_ASYNC[wtype](w, ctx, bg)
            else:
                raise ValueError(f"unknown widget type {wtype!r}")
        except Exception as err:
            ctx.note_error(key, str(err))
            tile = _draw_error(Image.new("RGB", (TUBE_W, TUBE_H), bg))
        if tile.size != (TUBE_W, TUBE_H):
            tile = tile.resize((TUBE_W, TUBE_H))
        strip.paste(tile, (i * TUBE_W, 0))
    return strip


async def screen_when_true(screen: Screen, ctx: RenderContext) -> bool:
    """Evaluate the screen's `when` template; True unless it renders 'true'."""
    if not screen.when:
        return True
    try:
        result = await ctx.renderer(screen.when)
    except Exception as err:
        ctx.note_error(f"{screen.name}:when", str(err))
        return False
    return str(result).strip().lower() == "true"
