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
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from PIL import Image, ImageDraw, ImageFont, ImageOps

try:
    from zoneinfo import ZoneInfo
except ImportError:  # no tz database on this system: analog_clock falls back to local time
    ZoneInfo = None

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
        # The screens file's top-level `font:` default (a family id or file:<name>);
        # widgets without their own `font` use this (set by the controller/editor)
        self.default_font: str | None = None
        # widget key -> last error message, so an error is logged once per change
        self.last_errors: dict[str, str] = {}
        # The editor's designer sets this to a dict to get, per custom widget key, the
        # bounding box of each item it drew
        self.record_boxes: dict[str, list] | None = None
        # (path, mtime) -> decoded picture, so a big photo isn't decoded on every refresh
        self.image_cache: dict[tuple, Image.Image] = {}
        # entity -> (monotonic time, daily forecast), so the forecast API isn't hit every
        # refresh; the clock is time.monotonic, injectable for tests
        self.forecast_cache: dict[str, tuple[float, list]] = {}
        self.now_monotonic = time.monotonic

    def font_family(self, w: dict) -> str | None:
        """The `font` field of a resolved widget, else the context's default."""
        fam = w.get("font") if isinstance(w, dict) else None
        if isinstance(fam, str) and fam and fam != "default":
            return fam
        return self.default_font

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
              start: int, bold: bool = False, min_size: int = 8,
              family: str | None = None) -> ImageFont.ImageFont:
    """Largest font size <= start whose text fits max_w x max_h."""
    size = max(start, min_size)
    while size > min_size:
        font = fonts.load_font(size, bold=bold, family=family)
        w, h = _text_size(draw, text, font)
        if w <= max_w and h <= max_h:
            return font
        size -= 1
    return fonts.load_font(min_size, bold=bold, family=family)


def _draw_centered(draw: ImageDraw.ImageDraw, cx: float, cy: float, text: str,
                   font, fill, align: str = "center") -> tuple[int, int, int, int]:
    """Draw text (several lines with \\n) around a point; returns the box it covers.

    cy is the middle of the text. `align` is both where cx sits (left edge, centre, right
    edge) and how the lines are lined up."""
    # Centre the ink, not the text origin: fonts leave space above the glyphs (bbox top), which
    # at large sizes pushes text down and off the tube
    if align not in ("left", "center", "right"):
        align = "center"
    x0, y0, x1, y1 = draw.textbbox((0, 0), text, font=font, align=align)
    ox = cx - x0 if align == "left" else cx - x1 if align == "right" else cx - (x0 + x1) / 2
    oy = cy - (y0 + y1) / 2
    draw.text((ox, oy), text, font=font, fill=fill, align=align)
    return (math.floor(ox + x0), math.floor(oy + y0), math.ceil(ox + x1), math.ceil(oy + y1))


def _str(value: Any) -> str:
    """A field as text. A typed backslash-n is a line break, so one-line inputs can do lines."""
    if value is None:
        return ""
    return str(value).replace("\\n", "\n")


def _num(value: Any, default: float) -> float:
    n = _parse_number(value)
    return default if n is None else n


def _size(value: Any) -> int | None:
    """A font size field: a number, or None for 'auto' / not set."""
    n = _parse_number(value)
    if n is None or n <= 0:
        return None
    return max(4, min(400, int(round(n))))


def _font(draw: ImageDraw.ImageDraw, text: str, size: int | None, max_w: int, max_h: int,
          start: int, bold: bool = False, family: str | None = None):
    """The font at a fixed `size`, or the largest one <= start that fits the box."""
    if size:
        return fonts.load_font(size, bold=bold, family=family)
    return _fit_font(draw, text, max_w, max_h, start=start, bold=bold, family=family)


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

# The template-capable fields of each widget type. Resolution itself walks every
# string of a widget (collect_templates), so a template anywhere works; this table
# is the documented contract the editor mirrors (editor.js FIELDS) and what tests
# check - every field listed here must survive collect_templates/resolve_templates.
# "font" (a family id or file:<name>) is on every widget that draws text.
_WIDGET_TEMPLATE_FIELDS = {
    "text": ("text", "color", "font"),
    "big_number": ("value", "unit", "label", "color", "font"),
    "ring": ("value", "max", "label", "unit", "color", "font"),
    "bar": ("value", "max", "label", "unit", "color", "font"),
    "icon": ("icon", "text", "color", "font"),
    "image": ("source",),
    "gif": ("source", "background"),
    "sparkline": ("entity", "hours", "unit", "color", "font"),
    "graph": ("entity", "hours", "style", "color", "label", "unit", "show_value",
              "min", "max", "points", "font"),
    "progress": ("value", "max", "label", "finish", "eta_format", "color", "track", "font"),
    "analog_clock": ("tz", "seconds", "numerals", "label", "color", "accent", "face", "font"),
    "weather": ("entity", "label", "show", "color", "icon_color", "font"),
    "ticker": ("text", "size", "color", "background", "y", "speed", "label", "font"),
    "custom": (),
    "blank": (),
}


def is_template(value: Any) -> bool:
    return isinstance(value, str) and ("{{" in value or "{%" in value)


def _walk_strings(obj: Any):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_strings(v)


def collect_templates(screen: Screen) -> list[str]:
    """All template strings in a screen (deduplicated, order preserved). Any string field of
    a widget, at any depth, may be a template."""
    found: list[str] = []
    seen: set[str] = set()

    def add(s: Any) -> None:
        if is_template(s) and s not in seen:
            seen.add(s)
            found.append(s)

    add(screen.when)
    add(screen.background)
    for s in _walk_strings(screen.strip):
        add(s)
    for s in _walk_strings(screen.canvas):
        add(s)
    for w in screen.tubes:
        for s in _walk_strings(w):
            add(s)
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


def _resolved_widget(w: Any, resolved: dict[str, str]) -> Any:
    """A copy of the widget with every template (at any depth) replaced by its result."""
    if isinstance(w, dict):
        return {k: _resolved_widget(v, resolved) for k, v in w.items()}
    if isinstance(w, list):
        return [_resolved_widget(v, resolved) for v in w]
    return _sub(w, resolved)


def _failed_templates(w: Any, resolved: dict[str, str], path: str = "") -> list[tuple[str, str]]:
    """(field path, error) for every template of the widget that failed to render."""
    out: list[tuple[str, str]] = []
    if isinstance(w, dict):
        for k, v in w.items():
            out += _failed_templates(v, resolved, f"{path}.{k}" if path else str(k))
    elif isinstance(w, list):
        for i, v in enumerate(w):
            out += _failed_templates(v, resolved, f"{path}[{i}]")
    elif isinstance(w, str) and resolved.get(w, "").startswith("__error__"):
        out.append((path, resolved[w][len("__error__"):]))
    return out


# ---------------------------------------------------------------------------
# widgets - each draws into a fresh 135x240 image
# ---------------------------------------------------------------------------

def draw_text(w: dict, ctx: RenderContext, bg) -> Image.Image:
    img = Image.new("RGB", (TUBE_W, TUBE_H), bg)
    draw = ImageDraw.Draw(img)
    text = _str(w.get("text"))
    color = parse_color(w.get("color"), DEFAULT_COLOR)
    align = str(w.get("align", "center")).lower()
    valign = str(w.get("valign", "middle")).lower()
    bold = str(w.get("weight", "")).lower() == "bold"
    family = ctx.font_family(w)
    size = _size(w.get("size"))
    pad = 8
    lines = text.split("\n")
    if size is None:
        # fit the whole block
        start = 120
        while start > 8:
            font = fonts.load_font(start, bold=bold, family=family)
            widths = [_text_size(draw, ln, font)[0] for ln in lines]
            line_h = _text_size(draw, "Ag", font)[1] + 4
            if max(widths, default=0) <= TUBE_W - 2 * pad and line_h * len(lines) <= TUBE_H - 2 * pad:
                break
            start -= 2
        font = fonts.load_font(max(start, 8), bold=bold, family=family)
    else:
        font = fonts.load_font(size, bold=bold, family=family)
    line_h = _text_size(draw, "Ag", font)[1] + 4
    total_h = line_h * len(lines)
    if valign == "top":
        y = pad
    elif valign == "bottom":
        y = TUBE_H - pad - total_h
    else:
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


def _bottom_label(draw: ImageDraw.ImageDraw, w: dict, default_size: int, legacy_cy: int,
                  default_color, family: str | None = None) -> int:
    """Draw a widget's label at the bottom of the tube. A one-line label sits where it always
    did; one with several lines is fitted into the bottom of the tube and grows upwards.
    Returns how many more pixels than a one-line label it took, so the caller can make room."""
    label = _str(w.get("label"))
    if not label:
        return 0
    color = parse_color(w.get("label_color"), default_color)
    size = _size(w.get("label_size"))
    if "\n" not in label:
        _draw_centered(draw, TUBE_W // 2, legacy_cy, label, fonts.load_font(size or default_size, family=family), color)
        return 0
    font = _font(draw, label, size, TUBE_W - 8, 100, start=default_size, family=family)
    h = _text_size(draw, label, font)[1]
    bottom = TUBE_H - 10
    _draw_centered(draw, TUBE_W // 2, bottom - h / 2, label, font, color)
    one_line_top = legacy_cy - default_size // 2
    return max(0, int(one_line_top - (bottom - h)))


def draw_big_number(w: dict, ctx: RenderContext, bg) -> Image.Image:
    img = Image.new("RGB", (TUBE_W, TUBE_H), bg)
    draw = ImageDraw.Draw(img)
    value = _str(w.get("value"))
    unit = _str(w.get("unit"))
    label = _str(w.get("label"))
    color = parse_color(w.get("color"), DEFAULT_COLOR)
    vcolor = parse_color(w.get("value_color"), color)
    ucolor = parse_color(w.get("unit_color"), color)
    vsize, usize = _size(w.get("value_size")), _size(w.get("unit_size"))
    family = ctx.font_family(w)
    extra = _bottom_label(draw, w, 18, TUBE_H - 34, color, family)
    up = extra // 2      # a tall label pushes the number up and leaves it less room
    cx = TUBE_W // 2
    if unit and w.get("unit_newline"):
        # The value alone gets the full width; the unit goes on its own line below it
        font = _font(draw, value, vsize, TUBE_W - 8, TUBE_H - 110 - extra, start=140, bold=True, family=family)
        _draw_centered(draw, cx, TUBE_H // 2 - 34 - up, value, font, vcolor)
        ufont = _font(draw, unit, usize, TUBE_W - 12, 40, start=36, bold=True, family=family)
        _draw_centered(draw, cx, TUBE_H // 2 + 46 - extra, unit, ufont, ucolor)
    elif not unit and not label:
        # Just a value (e.g. one digit of a time): as big as the tube allows
        font = _font(draw, value, vsize, TUBE_W - 6, TUBE_H - 24, start=240, bold=True, family=family)
        _draw_centered(draw, cx, TUBE_H // 2, value, font, vcolor)
    else:
        main = value + unit
        font = _font(draw, main, vsize, TUBE_W - 12, TUBE_H - 70 - extra, start=110, bold=True, family=family)
        _draw_centered(draw, cx, TUBE_H // 2 - 14 - up, main, font, vcolor)
    return img


def _gauge_fraction(w: dict) -> float:
    value = _parse_number(w.get("value"))
    mx = _parse_number(w.get("max")) or 100.0
    if value is None or mx <= 0:
        return 0.0
    return max(0.0, min(1.0, value / mx))


def _draw_arc(img: Image.Image, cx: float, cy: float, r: float, width: float, frac: float,
              color, track, start: float = -90, sweep: float = 360) -> tuple[int, int, int, int]:
    """A gauge ring of outer radius r: the `track` arc from `start` over `sweep` degrees (clockwise, 0 = three
    o'clock) with the first `frac` of it in `color`. Drawn at 4x and scaled down for smooth
    edges. Returns the box it covers."""
    ss = 4
    pad = 2
    half = int(math.ceil(r)) + pad
    size = 2 * half * ss
    layer = Image.new("L", (size, size), 0)
    mask_track = ImageDraw.Draw(layer)
    c = half * ss
    box = [c - r * ss, c - r * ss, c + r * ss, c + r * ss]
    wd = max(1, int(round(width * ss)))
    sweep = max(0.0, min(360.0, sweep))

    def arc(d: ImageDraw.ImageDraw, a0: float, a1: float) -> None:
        if a1 - a0 >= 359.999:
            d.ellipse(box, outline=255, width=wd)
        elif a1 > a0:
            d.arc(box, start=a0, end=a1, fill=255, width=wd)

    left, top = int(round(cx)) - half, int(round(cy)) - half
    if track is not None:
        arc(mask_track, start, start + sweep)
        img.paste(tuple(track), (left, top), layer.resize((2 * half, 2 * half), Image.LANCZOS))
    if frac > 0:
        layer2 = Image.new("L", (size, size), 0)
        arc(ImageDraw.Draw(layer2), start, start + sweep * min(1.0, frac))
        img.paste(tuple(color), (left, top), layer2.resize((2 * half, 2 * half), Image.LANCZOS))
    rr = int(math.ceil(r))
    return (int(round(cx)) - rr, int(round(cy)) - rr, int(round(cx)) + rr, int(round(cy)) + rr)


def draw_ring(w: dict, ctx: RenderContext, bg) -> Image.Image:
    img = Image.new("RGB", (TUBE_W, TUBE_H), bg)
    draw = ImageDraw.Draw(img)
    color = parse_color(w.get("color"), CLAUDE_ORANGE)
    vcolor = parse_color(w.get("value_color"), DEFAULT_COLOR)
    ucolor = parse_color(w.get("unit_color"), vcolor)
    lcolor = parse_color(w.get("label_color"), DEFAULT_COLOR)
    frac = _gauge_fraction(w)
    family = ctx.font_family(w)
    r = int(max(16, min(64, _num(w.get("radius"), 52))))
    width = int(max(2, min(r - 6, _num(w.get("thickness"), 12))))
    label = _str(w.get("label"))
    cx, cy = TUBE_W // 2, TUBE_H // 2 - 16
    if "\n" in label:
        cy = r + 10          # several lines of label: ring at the top, the label gets the rest
    _draw_arc(img, cx, cy, r, width, frac, color, TRACK_COLOR)
    value = _str(w.get("value"))
    unit = _str(w.get("unit"))
    vsize, usize, lsize = _size(w.get("value_size")), _size(w.get("unit_size")), _size(w.get("label_size"))
    inner = 2 * (r - width)      # room inside the ring
    if unit and w.get("unit_newline"):
        vfont = _font(draw, value, vsize, inner + 2, 44, start=48, bold=True, family=family)
        _draw_centered(draw, cx, cy - 8, value, vfont, vcolor)
        ufont = _font(draw, unit, usize, inner - 16, 20, start=20, bold=True, family=family)
        _draw_centered(draw, cx, cy + 24, unit, ufont, ucolor)
    else:
        vfont = _font(draw, value + unit, vsize, inner + 16, 40, start=34, bold=True, family=family)
        _draw_centered(draw, cx, cy, value + unit, vfont, vcolor)
    if label:
        if "\n" in label:
            top, bottom = cy + r + 8, TUBE_H - 8
            lfont = _font(draw, label, lsize, TUBE_W - 8, bottom - top, start=22, family=family)
            _draw_centered(draw, cx, (top + bottom) / 2, label, lfont, lcolor)
        else:
            _draw_centered(draw, cx, cy + r + 18, label, fonts.load_font(lsize or 16, family=family), lcolor)
    return img


def draw_bar(w: dict, ctx: RenderContext, bg) -> Image.Image:
    img = Image.new("RGB", (TUBE_W, TUBE_H), bg)
    draw = ImageDraw.Draw(img)
    color = parse_color(w.get("color"), CLAUDE_ORANGE)
    frac = _gauge_fraction(w)
    family = ctx.font_family(w)
    extra = _bottom_label(draw, w, 16, TUBE_H - 30, DEFAULT_COLOR, family)
    x0, x1 = 30, TUBE_W - 30
    y0, y1 = 24, TUBE_H - 56 - extra
    draw.rectangle([x0, y0, x1, y1], outline=TRACK_COLOR, width=6)
    fill_h = int((y1 - y0) * frac)
    if fill_h > 0:
        draw.rectangle([x0, y1 - fill_h, x1, y1], fill=color)
    value = _str(w.get("value")) + _str(w.get("unit"))
    vfont = _font(draw, value, _size(w.get("value_size")), TUBE_W - 16, 40, start=30, bold=True, family=family)
    _draw_centered(draw, TUBE_W // 2, (y0 + y1) // 2, value, vfont,
                   parse_color(w.get("value_color"), DEFAULT_COLOR))
    return img


def _draw_mdi(draw: ImageDraw.ImageDraw, cx: float, cy: float, icon: str, size: int,
              color) -> tuple[int, int, int, int]:
    name = icon[4:] if icon.startswith("mdi:") else icon
    cp = fonts.mdi_codepoint(name)
    mdi_font = fonts.load_mdi_font(size)
    if cp is not None and mdi_font is not None:
        return _draw_centered(draw, cx, cy, chr(cp), mdi_font, color)
    # no MDI font (e.g. tests) or an unknown name: draw a placeholder box
    h = size // 3
    draw.rectangle([cx - h, cy - h, cx + h, cy + h], outline=color, width=max(2, size // 16))
    return (int(cx - h), int(cy - h), int(cx + h), int(cy + h))


def draw_icon(w: dict, ctx: RenderContext, bg) -> Image.Image:
    img = Image.new("RGB", (TUBE_W, TUBE_H), bg)
    draw = ImageDraw.Draw(img)
    color = parse_color(w.get("color"), CLAUDE_ORANGE)
    text = _str(w.get("text"))
    family = ctx.font_family(w)
    # With text below, the icon sits in the upper part; on its own it is centred and a bit bigger
    cy = 88 if text else TUBE_H // 2
    size = _size(w.get("icon_size")) or (96 if text else 120)
    _draw_mdi(draw, TUBE_W // 2, cy, _str(w.get("icon")), size, color)
    if text:
        tfont = _font(draw, text, _size(w.get("text_size")), TUBE_W - 12, 60, start=24, family=family)
        _draw_centered(draw, TUBE_W // 2, 170, text, tfont,
                       parse_color(w.get("text_color"), DEFAULT_COLOR))
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
        return _decode(data)
    if source.startswith("entity_picture:"):
        if ctx.ha is None:
            raise ValueError("no HA client for entity_picture source")
        data = await ctx.ha.entity_picture(source[len("entity_picture:"):])
        if data is None:
            raise ValueError("entity has no entity_picture")
        return _decode(data)
    if source.startswith("file:"):
        path = image_path(ctx.images_dir, source[len("file:"):])
        key = (path, os.path.getmtime(path))
        cached = ctx.image_cache.get(key)
        if cached is None:
            with Image.open(path) as img:
                img.seek(0)     # first frame of an animated GIF
                cached = _upright(img).convert("RGBA")
            # Far more pixels than the tubes can show: keep a smaller copy
            cached.thumbnail((MAX_SOURCE_PX, MAX_SOURCE_PX), Image.LANCZOS)
            if len(ctx.image_cache) >= 12:
                ctx.image_cache.clear()
            ctx.image_cache[key] = cached
        return cached
    if source.startswith("http://") or source.startswith("https://"):
        if ctx.session is None:
            raise ValueError("no HTTP session for URL source")
        async with ctx.session.get(source, timeout=aiohttp_timeout()) as resp:
            if resp.status >= 400:
                raise ValueError(f"image GET {source} -> {resp.status}")
            data = await resp.read()
        return _decode(data)
    raise ValueError(f"unknown image source {source!r}")


MAX_SOURCE_PX = 1620      # twice the strip's width


def _upright(img: Image.Image) -> Image.Image:
    """Turn a photo the way its EXIF orientation says (phone photos are often stored sideways)."""
    try:
        return ImageOps.exif_transpose(img) or img
    except Exception:
        return img


def _decode(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    return _upright(img).convert("RGBA" if "A" in img.getbands() or "transparency" in img.info else "RGB")


IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")


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


def _fit_image(src: Image.Image, w: int, h: int, fit: str, zoom: float = 1.0,
               offset_x: float = 0.0, offset_y: float = 0.0, rotate: float = 0.0,
               bg=(0, 0, 0)) -> Image.Image:
    """Place a picture on a w x h canvas of colour `bg`.

    fit: "cover" fills the canvas (cropping), "contain" shows the whole picture. `zoom`
    multiplies that size, `offset_x`/`offset_y` move the picture's centre away from the
    canvas centre (canvas pixels, right/down positive), `rotate` turns it (degrees,
    anticlockwise). Transparent parts and anything the picture doesn't cover show `bg`."""
    if rotate % 360:
        src = src.convert("RGBA").rotate(rotate, expand=True, resample=Image.BICUBIC)
    sw, sh = src.size
    base = min(w / sw, h / sh) if fit == "contain" else max(w / sw, h / sh)
    scale = base * max(0.01, min(100.0, zoom))
    nw, nh = sw * scale, sh * scale
    left, top = (w - nw) / 2 + offset_x, (h - nh) / 2 + offset_y
    canvas = Image.new("RGB", (w, h), tuple(bg))
    # the part of the canvas the picture covers, and the matching part of the picture
    dx0, dy0 = max(0, round(left)), max(0, round(top))
    dx1, dy1 = min(w, round(left + nw)), min(h, round(top + nh))
    if dx1 <= dx0 or dy1 <= dy0:
        return canvas
    box = ((dx0 - left) / scale, (dy0 - top) / scale, (dx1 - left) / scale, (dy1 - top) / scale)
    box = (max(0.0, box[0]), max(0.0, box[1]), min(float(sw), box[2]), min(float(sh), box[3]))
    if box[2] - box[0] <= 0 or box[3] - box[1] <= 0:
        return canvas
    part = src.resize((dx1 - dx0, dy1 - dy0), Image.LANCZOS, box=box)
    if part.mode == "RGBA":
        canvas.paste(part, (dx0, dy0), part)
    else:
        canvas.paste(part.convert("RGB"), (dx0, dy0))
    return canvas


def _placed(src: Image.Image, w: dict, width: int, height: int, bg) -> Image.Image:
    """A picture placed by an image widget's (or the strip's) fit/zoom/offset/rotate fields."""
    return _fit_image(src, width, height, str(w.get("fit", "cover")),
                      zoom=_num(w.get("zoom"), 1.0),
                      offset_x=_num(w.get("offset_x"), 0.0), offset_y=_num(w.get("offset_y"), 0.0),
                      rotate=_num(w.get("rotate"), 0.0),
                      bg=parse_color(w.get("background"), bg))


async def draw_image(w: dict, ctx: RenderContext, bg) -> Image.Image:
    source = str(w.get("source") or "")
    bg = parse_color(w.get("background"), bg)
    if source == "builtin:claude":
        # starburst is drawn on the background
        return draw_builtin_claude(bg)
    src = await _fetch_image(source, ctx)
    return _placed(src, w, TUBE_W, TUBE_H, bg)


async def draw_gif(w: dict, ctx: RenderContext, bg) -> Image.Image:
    """A gif tube shows the GIF's FIRST frame, centred on the background (that is
    what the preview, old firmware and the editor show; on HA Tubes 2.4+ the clock
    plays the whole GIF itself - see controller.py)."""
    source = str(w.get("source") or "")
    bg = parse_color(w.get("background"), bg)
    img = Image.new("RGB", (TUBE_W, TUBE_H), bg)
    try:
        src = await _fetch_image(source, ctx)   # _fetch_image seeks to frame 0
    except Exception as err:
        ctx.note_error(f"gif {source}", str(err))
        return _draw_error(img)
    # centred, whole frame, no cropping (fit "contain" at zoom 1)
    tile = _fit_image(src, TUBE_W, TUBE_H, "contain", bg=bg)
    img.paste(tile, (0, 0))
    return img


# ---------------------------------------------------------------------------
# custom widget: a list of freely placed items
# ---------------------------------------------------------------------------

ITEM_TYPES = ("text", "ring", "bar", "icon", "image", "rect")
Box = tuple[int, int, int, int]


def _item_text(img: Image.Image, draw: ImageDraw.ImageDraw, it: dict,
               cw: int = TUBE_W, ch: int = TUBE_H, default_family: str | None = None) -> Box:
    text = _str(it.get("text"))
    x, y = _num(it.get("x"), cw / 2), _num(it.get("y"), ch / 2)
    align = str(it.get("align") or "center").lower()
    bold = str(it.get("weight") or "").lower() == "bold"
    fam = it.get("font")
    family = fam if isinstance(fam, str) and fam and fam != "default" else default_family
    max_w = int(_num(it.get("width"), cw - 8))
    max_h = int(_num(it.get("height"), 60))
    font = _font(draw, text, _size(it.get("size")), max_w, max_h, start=200, bold=bold, family=family)
    if not text:
        return (int(x) - 6, int(y) - 6, int(x) + 6, int(y) + 6)
    return _draw_centered(draw, x, y, text, font, parse_color(it.get("color"), DEFAULT_COLOR), align)


def _no_track(track: Any) -> bool:
    """track: none hides the unfilled part of a ring or bar."""
    return isinstance(track, str) and track.strip().lower() == "none"


def _item_ring(img: Image.Image, draw: ImageDraw.ImageDraw, it: dict,
               cw: int = TUBE_W, ch: int = TUBE_H, default_family: str | None = None) -> Box:
    x, y = _num(it.get("x"), cw / 2), _num(it.get("y"), ch / 2)
    r = max(4.0, _num(it.get("radius"), 50))
    width = max(1.0, min(r, _num(it.get("thickness"), 10)))
    track = it.get("track")
    track_color = None if _no_track(track) else parse_color(track, TRACK_COLOR)
    return _draw_arc(img, x, y, r, width, _gauge_fraction(it),
                     parse_color(it.get("color"), CLAUDE_ORANGE), track_color,
                     start=_num(it.get("start"), -90), sweep=_num(it.get("sweep"), 360))


def _item_bar(img: Image.Image, draw: ImageDraw.ImageDraw, it: dict,
              cw: int = TUBE_W, ch: int = TUBE_H, default_family: str | None = None) -> Box:
    x, y = _num(it.get("x"), cw / 2), _num(it.get("y"), ch / 2)
    bw, bh = max(2.0, _num(it.get("width"), 100)), max(2.0, _num(it.get("height"), 14))
    x0, y0, x1, y1 = round(x - bw / 2), round(y - bh / 2), round(x + bw / 2), round(y + bh / 2)
    radius = int(max(0, min(min(x1 - x0, y1 - y0) / 2, _num(it.get("radius"), min(bw, bh) / 2))))
    direction = str(it.get("direction") or ("up" if bh > bw else "right")).lower()
    track = it.get("track")
    if not _no_track(track):
        draw.rounded_rectangle([x0, y0, x1, y1], radius=radius, fill=parse_color(track, TRACK_COLOR))
    frac = _gauge_fraction(it)
    if frac > 0:
        # The filled part is the whole rounded bar, cut off at the value
        mask = Image.new("L", (x1 - x0 + 1, y1 - y0 + 1), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, x1 - x0, y1 - y0], radius=radius, fill=255)
        cut = Image.new("L", mask.size, 0)
        mw, mh = mask.size
        if direction == "up":
            cut.paste(255, (0, mh - round(mh * frac), mw, mh))
        elif direction == "down":
            cut.paste(255, (0, 0, mw, round(mh * frac)))
        elif direction == "left":
            cut.paste(255, (mw - round(mw * frac), 0, mw, mh))
        else:
            cut.paste(255, (0, 0, round(mw * frac), mh))
        from PIL import ImageChops
        img.paste(parse_color(it.get("color"), CLAUDE_ORANGE), (x0, y0), ImageChops.multiply(mask, cut))
    return (x0, y0, x1, y1)


def _item_icon(img: Image.Image, draw: ImageDraw.ImageDraw, it: dict,
               cw: int = TUBE_W, ch: int = TUBE_H, default_family: str | None = None) -> Box:
    x, y = _num(it.get("x"), cw / 2), _num(it.get("y"), ch / 2)
    return _draw_mdi(draw, x, y, _str(it.get("icon")), _size(it.get("size")) or 64,
                     parse_color(it.get("color"), CLAUDE_ORANGE))


def _item_rect(img: Image.Image, draw: ImageDraw.ImageDraw, it: dict,
               cw: int = TUBE_W, ch: int = TUBE_H, default_family: str | None = None) -> Box:
    x, y = _num(it.get("x"), cw / 2), _num(it.get("y"), ch / 2)
    bw, bh = max(1.0, _num(it.get("width"), 100)), max(1.0, _num(it.get("height"), 2))
    x0, y0, x1, y1 = round(x - bw / 2), round(y - bh / 2), round(x + bw / 2), round(y + bh / 2)
    radius = int(max(0, min(min(x1 - x0, y1 - y0) / 2, _num(it.get("radius"), 0))))
    draw.rounded_rectangle([x0, y0, x1 - 1, y1 - 1], radius=radius,
                           fill=parse_color(it.get("color"), DEFAULT_COLOR))
    return (x0, y0, x1, y1)


async def _item_image(img: Image.Image, it: dict, ctx: RenderContext, bg,
                      cw: int = TUBE_W, ch: int = TUBE_H, default_family: str | None = None) -> Box:
    x, y = _num(it.get("x"), cw / 2), _num(it.get("y"), ch / 2)
    bw = int(max(1, min(cw * 2, _num(it.get("width"), cw))))
    bh = int(max(1, min(ch * 2, _num(it.get("height"), ch))))
    x0, y0 = round(x - bw / 2), round(y - bh / 2)
    source = str(it.get("source") or "")
    if source == "builtin:claude":
        tile = _fit_image(draw_builtin_claude(parse_color(it.get("background"), bg)), bw, bh, "contain",
                          bg=parse_color(it.get("background"), bg))
    else:
        opts = dict(it)
        opts.setdefault("fit", "contain")
        tile = _placed(await _fetch_image(source, ctx), opts, bw, bh, bg)
    img.paste(tile, (x0, y0))
    return (x0, y0, x0 + bw, y0 + bh)


_ITEM_DRAW = {"text": _item_text, "ring": _item_ring, "bar": _item_bar, "icon": _item_icon,
              "rect": _item_rect}


async def draw_items(items: object, ctx: RenderContext, img: Image.Image, bg,
                     key: str, cw: int = TUBE_W, ch: int = TUBE_H) -> None:
    """Draw a list of custom items (in order, later ones on top) onto an existing
    cw x ch image, each placed by its own x/y in canvas pixels (0,0 = top left).
    An item that fails shows a small red "!". Records the boxes under `key` when
    ctx.record_boxes is set."""
    draw = ImageDraw.Draw(img)
    boxes: list[Box] = []
    for i, it in enumerate(items if isinstance(items, list) else []):
        try:
            if not isinstance(it, dict):
                raise ValueError("item must be a mapping")
            kind = it.get("type")
            if kind == "image":
                box = await _item_image(img, it, ctx, bg, cw, ch)
                draw = ImageDraw.Draw(img)
            elif kind in _ITEM_DRAW:
                box = _ITEM_DRAW[kind](img, draw, it, cw, ch, ctx.default_font)
            else:
                raise ValueError(f"unknown item type {kind!r} (expected one of {', '.join(ITEM_TYPES)})")
        except Exception as err:
            ctx.note_error(f"{key} item {i}", str(err))
            x = _num(it.get("x"), cw / 2) if isinstance(it, dict) else cw / 2
            y = _num(it.get("y"), ch / 2) if isinstance(it, dict) else ch / 2
            box = _draw_centered(draw, x, y, "!", fonts.load_font(24, bold=True), (255, 80, 80))
        boxes.append(tuple(int(v) for v in box))
    if ctx.record_boxes is not None:
        ctx.record_boxes[key] = boxes


async def draw_custom(w: dict, ctx: RenderContext, bg, width: int = TUBE_W, height: int = TUBE_H) -> Image.Image:
    """Items drawn in order (later ones on top), each placed by its own x/y in canvas pixels
    (0,0 = top left; a tube is 135 x 240, a canvas screen 810 x 240). An item that fails
    shows a small red "!"."""
    bg = parse_color(w.get("background"), bg)
    img = Image.new("RGB", (width, height), bg)
    key = str(w.get("_key") or "custom")
    await draw_items(w.get("items"), ctx, img, bg, key, width, height)
    return img


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
    family = ctx.font_family(w)
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
        vfont = fonts.load_font(20, bold=True, family=family)
        _draw_centered(draw, TUBE_W // 2, 20, latest, vfont, DEFAULT_COLOR)
    else:
        _draw_centered(draw, TUBE_W // 2, (top + bottom) // 2, "no data",
                       fonts.load_font(16, family=family), DEFAULT_COLOR)
    return img


# ---------------------------------------------------------------------------
# graph: history graph of one entity (the bigger brother of sparkline)
# ---------------------------------------------------------------------------

def _history_values(states: list, hours: float) -> list[float]:
    """Numeric states of a history reply, oldest first; non-numeric states are skipped."""
    values: list[float] = []
    for s in states or []:
        if not isinstance(s, dict):
            continue
        v = _parse_number(s.get("state"))
        if v is not None:
            values.append(v)
    return values


def _resample(values: list[float], max_points: int) -> list[float]:
    """Downsample to at most max_points values (mean of each bucket), keeping first and last."""
    n = len(values)
    if max_points <= 0 or n <= max_points:
        return list(values)
    out: list[float] = []
    for i in range(max_points):
        lo = i * n // max_points
        hi = (i + 1) * n // max_points
        bucket = values[lo:max(hi, lo + 1)]
        out.append(sum(bucket) / len(bucket))
    return out


async def draw_graph(w: dict, ctx: RenderContext, bg) -> Image.Image:
    """History graph of one entity: line, area (line + 35% fill) or bars (mean per bucket).

    The axis is auto from the data unless min/max are given; the min/max values are drawn
    small at the left, the label at the top and (show_value) the latest value big under it."""
    img = Image.new("RGB", (TUBE_W, TUBE_H), bg)
    draw = ImageDraw.Draw(img)
    color = parse_color(w.get("color"), CLAUDE_ORANGE)
    entity = str(w.get("entity") or "")
    hours = _num(w.get("hours"), 24.0)
    if hours <= 0:
        hours = 24.0
    style = str(w.get("style") or "area").lower()
    if style not in ("line", "area", "bars"):
        style = "area"
    label = _str(w.get("label"))
    unit = _str(w.get("unit"))
    show_value = w.get("show_value", True)
    if isinstance(show_value, str):
        show_value = show_value.strip().lower() not in ("false", "0", "no", "off")
    points = int(max(2, min(600, _num(w.get("points"), 12 if style == "bars" else 120))))
    if ctx.ha is None:
        raise ValueError("no HA client for graph")
    states = await ctx.ha.history(entity, hours)
    values = _history_values(states, hours)

    top = 46 if (label or show_value) else 16
    bottom = TUBE_H - 14
    left, right = 26, TUBE_W - 8     # left margin leaves room for the axis values
    family = ctx.font_family(w)
    if label:
        _draw_centered(draw, TUBE_W // 2, 14, label, fonts.load_font(14, family=family),
                       parse_color(w.get("label_color"), DEFAULT_COLOR))
    if not values:
        _draw_centered(draw, TUBE_W // 2, (top + bottom) // 2, "no data",
                       fonts.load_font(16, family=family), DEFAULT_COLOR)
        return img

    lo = _parse_number(w.get("min"))
    hi = _parse_number(w.get("max"))
    if lo is None:
        lo = min(values)
    if hi is None:
        hi = max(values)
    if hi - lo < 1e-9:
        hi = lo + 1.0

    def xy(v: float, i: int, n: int) -> tuple[float, float]:
        x = left + (right - left) * (i / max(n - 1, 1))
        y = bottom - (bottom - top) * (v - lo) / (hi - lo)
        return x, y

    # faint baseline and the axis values small at the left
    draw.line([(left, bottom), (right, bottom)], fill=(50, 50, 50), width=1)
    axis_font = fonts.load_font(11, family=family)
    axis_color = (150, 150, 150)
    _draw_centered(draw, 2, top + 6, f"{hi:g}", axis_font, axis_color, align="left")
    _draw_centered(draw, 2, bottom - 6, f"{lo:g}", axis_font, axis_color, align="left")

    if style == "bars":
        n = max(1, min(points, len(values)))
        buckets: list[list[float]] = [[] for _ in range(n)]
        for i, v in enumerate(values):
            buckets[min(n - 1, i * n // len(values))].append(v)
        means = [sum(b) / len(b) for b in buckets if b]
        if not means:
            _draw_centered(draw, TUBE_W // 2, (top + bottom) // 2, "no data",
                           fonts.load_font(16, family=family), DEFAULT_COLOR)
            return img
        bw = (right - left) / n
        for i, m in enumerate(means):
            x0 = left + i * bw + 1
            x1 = left + (i + 1) * bw - 1
            _, y = xy(m, 0, 1)
            draw.rectangle([x0, y, x1, bottom], fill=color)
    else:
        pts = [xy(v, i, len(values)) for i, v in enumerate(_resample(values, points))]
        if style == "area":
            # translucent fill: blend the colour 35% into the background
            fill = tuple(int(c * 0.35 + b * 0.65) for c, b in zip(color, tuple(bg)))
            draw.polygon(pts + [(pts[-1][0], bottom), (pts[0][0], bottom)], fill=fill)
        draw.line(pts, fill=color, width=2, joint="curve")
        draw.ellipse([pts[-1][0] - 3, pts[-1][1] - 3, pts[-1][0] + 3, pts[-1][1] + 3],
                     fill=color)

    if show_value:
        latest = f"{values[-1]:g}" + unit
        vfont = _font(draw, latest, _size(w.get("value_size")), TUBE_W - 8, 34, start=30, bold=True, family=family)
        _draw_centered(draw, TUBE_W // 2, top - 22, latest, vfont,
                       parse_color(w.get("value_color"), DEFAULT_COLOR))
    return img


# ---------------------------------------------------------------------------
# progress: a 270 degree gauge with an ETA in the gap at the bottom
# ---------------------------------------------------------------------------

_ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?"
    r"(Z|[+-]\d{2}:?\d{2})?$")
_HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _parse_finish(value: Any, now: datetime) -> datetime | None:
    """A finish time as ISO datetime (tz-aware, or naive = local), unix timestamp, or
    "HH:MM" (today, or tomorrow when that time has already passed). None when unparseable."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if _HHMM_RE.match(s):
        h, m = (int(x) for x in _HHMM_RE.match(s).groups())
        t = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if t <= now:
            t += timedelta(days=1)
        return t
    n = _parse_number(s)
    if n is not None and re.fullmatch(r"-?\d+(\.\d+)?", s):
        try:
            return datetime.fromtimestamp(n, tz=timezone.utc).astimezone(now.tzinfo or None)
        except (OverflowError, OSError, ValueError):
            return None
    if _ISO_RE.match(s):
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=now.tzinfo)   # naive ISO = local time
        return dt
    return None


def format_eta(finish_value: str, now: datetime, fmt: str) -> str:
    """The ETA text for a progress widget.

    fmt "remaining": "23 min", "1 h 05", "done" when the finish is past (or <= 0).
    fmt "time": "12:45" (local time of the finish). Garbage input -> ""."""
    finish = _parse_finish(finish_value, now)
    if finish is None:
        return ""
    # a naive `now` (datetime.now()) and a tz-aware finish can't be subtracted: make
    # them match - an aware finish is read in local time, a naive one in now's zone
    if finish.tzinfo is not None and now.tzinfo is None:
        finish = finish.astimezone().replace(tzinfo=None)
    elif finish.tzinfo is None and now.tzinfo is not None:
        finish = finish.replace(tzinfo=now.tzinfo)
    delta = (finish - now).total_seconds()
    if fmt == "time":
        return finish.astimezone(now.tzinfo or None).strftime("%H:%M") if delta > 0 else "done"
    if delta <= 0:
        return "done"
    minutes = int(round(delta / 60))
    if minutes < 1:
        return "<1 min"
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02d}"


def draw_progress(w: dict, ctx: RenderContext, bg) -> Image.Image:
    """270 degree arc (open at the bottom) with the percentage in the middle, the label
    under it and the ETA text in the gap at the bottom."""
    img = Image.new("RGB", (TUBE_W, TUBE_H), bg)
    draw = ImageDraw.Draw(img)
    color = parse_color(w.get("color"), CLAUDE_ORANGE)
    track = parse_color(w.get("track"), (0x33, 0x33, 0x33))
    frac = _gauge_fraction(w)
    family = ctx.font_family(w)
    label = _str(w.get("label"))
    r = 62
    width = 12
    cx, cy = TUBE_W // 2, 108
    # 270 degrees open at the bottom: from 135 deg to 405 deg (0 = three o'clock, clockwise)
    _draw_arc(img, cx, cy, r, width, frac, color, track, start=135, sweep=270)
    pct = f"{round(frac * 100)}%"
    vfont = _font(draw, pct, _size(w.get("value_size")), 2 * (r - width), 44, start=40, bold=True, family=family)
    _draw_centered(draw, cx, cy - 6, pct, vfont, parse_color(w.get("value_color"), DEFAULT_COLOR))
    if label:
        _draw_centered(draw, cx, cy + 22, label, fonts.load_font(_size(w.get("label_size")) or 16, family=family),
                       parse_color(w.get("label_color"), DEFAULT_COLOR))
    eta = format_eta(_str(w.get("finish")), datetime.now(), str(w.get("eta_format") or "remaining"))
    if eta:
        _draw_centered(draw, cx, TUBE_H - 26, eta, fonts.load_font(18, bold=True, family=family),
                       parse_color(w.get("eta_color"), color))
    return img


# ---------------------------------------------------------------------------
# analog_clock: an analogue clock face, drawn at 3x and downscaled
# ---------------------------------------------------------------------------

def hand_angles(dt: datetime) -> tuple[float, float, float]:
    """(hour, minute, second) hand angles in degrees, 0 = twelve o'clock, clockwise."""
    second = dt.second + dt.microsecond / 1e6
    minute = dt.minute + second / 60
    hour = (dt.hour % 12) + minute / 60
    return (hour * 30.0, minute * 6.0, second * 6.0)


def _tzinfo(name: Any):
    """A zoneinfo.ZoneInfo for an IANA name, or None (local time) when unknown/absent."""
    if ZoneInfo is None or not name:
        return None
    try:
        return ZoneInfo(str(name))
    except Exception:
        return None


def draw_analog_clock(w: dict, ctx: RenderContext, bg) -> Image.Image:
    """Analogue clock face at datetime.now(tz): face circle, 12 ticks (or 12/3/6/9 numerals),
    hour/minute hands, an optional seconds hand, and a small label under the face."""
    ss = 3   # draw at 3x and downsample, like the ring's 4x arcs, for smooth edges
    img = Image.new("RGB", (TUBE_W * ss, TUBE_H * ss), tuple(bg))
    draw = ImageDraw.Draw(img)
    color = parse_color(w.get("color"), DEFAULT_COLOR)
    accent = parse_color(w.get("accent"), CLAUDE_ORANGE)
    face = parse_color(w.get("face"), (0x22, 0x22, 0x22))
    seconds = w.get("seconds", True)
    if isinstance(seconds, str):
        seconds = seconds.strip().lower() not in ("false", "0", "no", "off")
    numerals = bool(w.get("numerals", False))
    family = ctx.font_family(w) if ctx is not None else w.get("font")
    now = datetime.now(_tzinfo(w.get("tz")))
    cx, cy = TUBE_W * ss / 2, 104 * ss
    r = 78 * ss
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=face, outline=color, width=2 * ss)
    # ticks (or numerals at 12/3/6/9)
    num_font = fonts.load_font(20 * ss, bold=True, family=family)
    for i in range(12):
        a = math.radians(i * 30)
        if numerals and i % 3 == 0:
            label = str(12 if i == 0 else i // 3 * 3)
            tx = cx + math.sin(a) * (r - 16 * ss)
            ty = cy - math.cos(a) * (r - 16 * ss)
            _draw_centered(draw, tx, ty, label, num_font, color)
        else:
            x0 = cx + math.sin(a) * (r - 6 * ss)
            y0 = cy - math.cos(a) * (r - 6 * ss)
            x1 = cx + math.sin(a) * (r - (14 if i % 3 == 0 else 10) * ss)
            y1 = cy - math.cos(a) * (r - (14 if i % 3 == 0 else 10) * ss)
            draw.line([(x0, y0), (x1, y1)], fill=color, width=(3 if i % 3 == 0 else 2) * ss)
    hour_deg, minute_deg, second_deg = hand_angles(now)

    def hand(deg: float, length: float, width: float, col) -> None:
        a = math.radians(deg)
        draw.line([(cx, cy),
                   (cx + math.sin(a) * length, cy - math.cos(a) * length)],
                  fill=col, width=int(width * ss))
        draw.ellipse([cx - 3 * ss, cy - 3 * ss, cx + 3 * ss, cy + 3 * ss], fill=col)

    hand(hour_deg, r * 0.5, 5, color)
    hand(minute_deg, r * 0.78, 3.5, color)
    if seconds:
        hand(second_deg, r * 0.86, 1.5, accent)
    label = _str(w.get("label"))
    if label:
        _draw_centered(draw, cx, 208 * ss, label, fonts.load_font(16 * ss, family=family),
                       parse_color(w.get("label_color"), DEFAULT_COLOR))
    return img.resize((TUBE_W, TUBE_H), Image.LANCZOS)


# ---------------------------------------------------------------------------
# weather: current conditions of a weather.* entity
# ---------------------------------------------------------------------------

# HA condition -> mdi icon (unknown conditions fall back to cloudy)
WEATHER_ICONS = {
    "sunny": "mdi:weather-sunny",
    "cloudy": "mdi:weather-cloudy",
    "partlycloudy": "mdi:weather-partly-cloudy",
    "rainy": "mdi:weather-rainy",
    "pouring": "mdi:weather-pouring",
    "snowy": "mdi:weather-snowy",
    "snowy-rainy": "mdi:weather-snowy-rainy",
    "fog": "mdi:weather-fog",
    "hail": "mdi:weather-hail",
    "lightning": "mdi:weather-lightning",
    "lightning-rainy": "mdi:weather-lightning-rainy",
    "windy": "mdi:weather-windy",
    "windy-variant": "mdi:weather-windy-variant",
    "clear-night": "mdi:weather-night",
    "exceptional": "mdi:alert-circle-outline",
}
FORECAST_CACHE_SECONDS = 600   # 10 minutes


async def _daily_forecast(entity: str, ctx: RenderContext) -> list[dict]:
    """The daily forecast of a weather entity, cached per entity for 10 minutes."""
    if ctx.ha is None:
        return []
    now = ctx.now_monotonic()
    cached = ctx.forecast_cache.get(entity)
    if cached is not None and now - cached[0] < FORECAST_CACHE_SECONDS:
        return cached[1]
    try:
        forecast = await ctx.ha.forecast_daily(entity)
    except Exception as err:
        ctx.note_error(f"weather:{entity}", str(err))
        forecast = []
    ctx.forecast_cache[entity] = (now, forecast)
    return forecast


async def draw_weather(w: dict, ctx: RenderContext, bg) -> Image.Image:
    """Current weather: big condition icon in the top half, temperature big, today's
    high/low small (show: today) and the label at the bottom."""
    img = Image.new("RGB", (TUBE_W, TUBE_H), bg)
    draw = ImageDraw.Draw(img)
    entity = str(w.get("entity") or "")
    color = parse_color(w.get("color"), DEFAULT_COLOR)
    icon_color = parse_color(w.get("icon_color"), (0xFF, 0xC1, 0x07))
    show = str(w.get("show") or "today").lower()
    if show not in ("today", "now"):
        show = "today"
    label = _str(w.get("label"))
    family = ctx.font_family(w)
    if ctx.ha is None:
        raise ValueError("no HA client for weather")
    st = await ctx.ha.state(entity)
    if not st:
        _draw_centered(draw, TUBE_W // 2, TUBE_H // 2, "no data", fonts.load_font(16, family=family), DEFAULT_COLOR)
        return img
    condition = str(st.get("state") or "").strip().lower()
    icon = WEATHER_ICONS.get(condition, WEATHER_ICONS["cloudy"])
    attrs = st.get("attributes") if isinstance(st.get("attributes"), dict) else {}
    temp = _parse_number(attrs.get("temperature"))
    unit = attrs.get("temperature_unit") or "°"
    _draw_mdi(draw, TUBE_W // 2, 62, icon, 84, icon_color)
    if temp is not None:
        text = f"{temp:g}{unit if unit != '°' else '°'}"
        tfont = _font(draw, text, _size(w.get("temp_size")), TUBE_W - 8, 60, start=44, bold=True, family=family)
        _draw_centered(draw, TUBE_W // 2, 140, text, tfont, color)
    else:
        _draw_centered(draw, TUBE_W // 2, 140, "--", fonts.load_font(36, bold=True, family=family), color)
    if show == "today":
        forecast = await _daily_forecast(entity, ctx)
        today = None
        today_str = datetime.now().date().isoformat()
        for f in forecast:
            dt = str(f.get("datetime") or "")[:10]
            if dt == today_str:
                today = f
                break
        if today is None and forecast:
            today = forecast[0]     # no entry for today: the first one is the closest
        if today is not None:
            hi = _parse_number(today.get("temperature"))
            lo = _parse_number(today.get("templow"))
            if hi is not None or lo is not None:
                hl = f"H {hi:g}{unit if hi is not None else ''} L {lo:g}{unit if lo is not None else ''}".replace("None", "--")
                _draw_centered(draw, TUBE_W // 2, 178, hl, fonts.load_font(18, family=family),
                               parse_color(w.get("hl_color"), DEFAULT_COLOR))
    if label:
        _draw_centered(draw, TUBE_W // 2, TUBE_H - 22, label, fonts.load_font(16, family=family),
                       parse_color(w.get("label_color"), DEFAULT_COLOR))
    return img


# ---------------------------------------------------------------------------
# ticker: a scrolling text band (the animation engine uses ticker_band/ticker_cycle)
# ---------------------------------------------------------------------------

TICKER_GAP = 60   # px of empty band between two loops of the text


def ticker_band(w: dict, offset: float, family: str | None = None) -> tuple[Image.Image, tuple[int, int, int, int]]:
    """The ticker's band image at scroll `offset` px, and its (x, y, w, h) in the tube.

    `w` is the resolved widget dict. The band is TUBE_W x (size + 12) centred at y; the
    text is drawn twice with a TICKER_GAP px gap so it loops seamlessly. Text narrower
    than the tube is centred and does not scroll."""
    size = _size(w.get("size")) or 28
    band_h = size + 12
    y = int(round(_num(w.get("y"), TUBE_H / 2)))
    top = max(0, min(TUBE_H - band_h, y - band_h // 2))
    band = Image.new("RGB", (TUBE_W, band_h), parse_color(w.get("background"), (0, 0, 0)))
    draw = ImageDraw.Draw(band)
    text = _str(w.get("text"))
    color = parse_color(w.get("color"), DEFAULT_COLOR)
    font = fonts.load_font(size, bold=True, family=family)
    tw = _text_size(draw, text, font)[0] if text else 0
    if not text:
        return band, (0, top, TUBE_W, band_h)
    if tw <= TUBE_W:
        _draw_centered(draw, TUBE_W / 2, band_h / 2, text, font, color)
    else:
        # scroll left: draw the text at -offset and again one cycle later
        cycle = tw + TICKER_GAP
        start = -int(round(offset)) % cycle
        x = start - cycle
        while x < TUBE_W:
            draw.text((x, 0), text, font=font, fill=color)
            x += cycle
    return band, (0, top, TUBE_W, band_h)


def ticker_cycle(w: dict, family: str | None = None) -> float:
    """Seconds for one full loop of the ticker: (text width + gap) / speed.
    0 when the text fits the tube (it is centred and doesn't scroll)."""
    speed = _num(w.get("speed"), 40.0)
    if speed <= 0:
        return 0.0
    probe = Image.new("RGB", (1, 1))
    size = _size(w.get("size")) or 28
    tw = _text_size(ImageDraw.Draw(probe), _str(w.get("text")), fonts.load_font(size, bold=True, family=family))[0]
    if tw <= TUBE_W:
        return 0.0
    return (tw + TICKER_GAP) / speed


def draw_ticker(w: dict, ctx: RenderContext, bg) -> Image.Image:
    """The static ticker: the label above the band plus the band at scroll offset 0.

    The band colour defaults to the screen background, so the band is only visible
    where the text is; ticker_band itself takes the colour from w["background"]."""
    w = dict(w)
    if w.get("background") is None:
        w["background"] = "#%02X%02X%02X" % tuple(max(0, min(255, int(c))) for c in tuple(bg))
    img = Image.new("RGB", (TUBE_W, TUBE_H), bg)
    family = ctx.font_family(w)
    label = _str(w.get("label"))
    if label:
        draw = ImageDraw.Draw(img)
        y = int(round(_num(w.get("y"), TUBE_H / 2)))
        size = _size(w.get("size")) or 28
        band_h = size + 12
        top = max(0, min(TUBE_H - band_h, y - band_h // 2))
        _draw_centered(draw, TUBE_W // 2, max(12, top - 12), label, fonts.load_font(14, family=family),
                       parse_color(w.get("label_color"), DEFAULT_COLOR))
    band, (x, y, bw, bh) = ticker_band(w, 0, family)
    img.paste(band, (x, y))
    return img


def draw_blank(w: dict, ctx: RenderContext, bg) -> Image.Image:
    return Image.new("RGB", (TUBE_W, TUBE_H), bg)


_WIDGET_DRAW = {
    "text": draw_text,
    "big_number": draw_big_number,
    "ring": draw_ring,
    "bar": draw_bar,
    "icon": draw_icon,
    "progress": draw_progress,
    "analog_clock": draw_analog_clock,
    "ticker": draw_ticker,
    "blank": draw_blank,
}
_WIDGET_DRAW_ASYNC = {
    "custom": draw_custom,
    "image": draw_image,
    "gif": draw_gif,
    "sparkline": draw_sparkline,
    "graph": draw_graph,
    "weather": draw_weather,
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
        failed = _failed_templates(screen.strip, resolved)
        if failed:
            ctx.note_error(f"{screen.name}:strip", f"template failed for {failed[0][0]}: {failed[0][1]}")
            return _draw_error(strip)
        w = _resolved_widget(screen.strip, resolved)
        try:
            source = str(w.get("source") or "")
            sbg = parse_color(w.get("background"), bg)
            if source == "builtin:claude":
                strip = _fit_image(draw_builtin_claude(sbg), STRIP_W, STRIP_H, "contain", bg=sbg)
            else:
                strip = _placed(await _fetch_image(source, ctx), w, STRIP_W, STRIP_H, bg)
        except Exception as err:
            ctx.note_error(f"{screen.name}:strip", str(err))
            strip = Image.new("RGB", (STRIP_W, STRIP_H), bg)
            _draw_error(strip)
        return strip

    if screen.canvas is not None:
        # A free design across all six tubes: the same items a custom tube is made
        # of, drawn straight into the 810x240 strip at strip coordinates
        key = f"{screen.name}:canvas"
        failed = _failed_templates(screen.canvas, resolved)
        if failed:
            ctx.note_error(key, f"template failed for {failed[0][0]}: {failed[0][1]}")
            return _draw_error(strip)
        w = _resolved_widget(screen.canvas, resolved)
        try:
            cbg = parse_color(w.get("background"), bg)
            strip = Image.new("RGB", (STRIP_W, STRIP_H), cbg)
            await draw_items(w.get("items"), ctx, strip, cbg, key, STRIP_W, STRIP_H)
        except Exception as err:
            ctx.note_error(key, str(err))
            strip = Image.new("RGB", (STRIP_W, STRIP_H), bg)
            _draw_error(strip)
        return strip

    for i, raw_w in enumerate(screen.tubes):
        key = f"{screen.name}:tube{i}"
        w = _resolved_widget(raw_w, resolved)
        tile = None
        try:
            failed = _failed_templates(raw_w, resolved)
            if failed:
                # Show that the data is broken rather than quietly drawing an empty tube
                raise ValueError(f"template failed for {', '.join(f for f, _ in failed)}: {failed[0][1]}")
            w["_key"] = key
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


def _wrap_lines(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> list[str]:
    """Greedy word wrap of `text` to lines that fit max_w with `font`."""
    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    line = words[0]
    for word in words[1:]:
        if _text_size(draw, line + " " + word, font)[0] <= max_w:
            line += " " + word
        else:
            lines.append(line)
            line = word
    lines.append(line)
    return lines


def render_message(message: str, ctx: RenderContext, icon: str | None = None,
                   color: str | None = None, background: str = "#000000") -> Image.Image:
    """A flash message across the whole 810x240 strip.

    With an icon, the icon is centred on tube 0 (x 0..135) and the text uses
    x 135..810; without one the text spans the full strip. The text is wrapped
    onto up to 3 lines and auto-sized with _fit_font to fill the available box
    (8 px margins), centred, in `color` (default white) on `background`."""
    bg = parse_color(background, (0, 0, 0))
    fill = parse_color(color, DEFAULT_COLOR)
    img = Image.new("RGB", (STRIP_W, STRIP_H), bg)
    draw = ImageDraw.Draw(img)
    text_x0 = TUBE_W if icon else 0
    box_w = STRIP_W - text_x0 - 2 * 8
    box_h = STRIP_H - 2 * 8
    if icon:
        # same MDI helpers draw_icon uses; the glyph as large as the tube allows
        probe = _fit_font(draw, "W", TUBE_W - 16, TUBE_H - 16, start=200)
        _draw_mdi(draw, TUBE_W // 2, TUBE_H // 2, icon, _text_size(draw, "W", probe)[1] or 120, fill)
    # Wrap at decreasing sizes until the text fits on at most 3 lines, then let
    # _fit_font size the block to fill the box
    text = _str(message)
    size = 120
    while size > 8:
        f = fonts.load_font(size)
        wrapped = _wrap_lines(draw, text, f, box_w)
        if len(wrapped) <= 3 and _text_size(draw, "\n".join(wrapped), f)[1] <= box_h:
            break
        size -= 4
    lines = _wrap_lines(draw, text, fonts.load_font(max(size, 8)), box_w)[:3]
    font = _fit_font(draw, "\n".join(lines), box_w, box_h, start=120)
    _draw_centered(draw, text_x0 + (STRIP_W - text_x0) / 2, STRIP_H / 2,
                   "\n".join(lines), font, fill)
    return img


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


async def resolved_tubes(screen: Screen, ctx: RenderContext) -> list[dict | None]:
    """The screen's six tube widget dicts with their templates resolved exactly as
    render_screen does it (same resolve_templates + _resolved_widget pass), for
    consumers that need the widgets themselves (e.g. animate.py's animators).
    Strip and canvas screens have no per-tube widgets: a list of six None."""
    if screen.strip is not None or screen.canvas is not None:
        return [None] * 6
    resolved = await resolve_templates(screen, ctx)
    return [_resolved_widget(w, resolved) for w in screen.tubes]


def is_photo_source(source: Any) -> bool:
    """A camera / entity_picture / http(s) source: a photo, which JPEG often
    compresses much better than QOI (the clock decides per upload)."""
    return isinstance(source, str) and (
        source.startswith("camera:") or source.startswith("entity_picture:")
        or source.startswith("http://") or source.startswith("https://"))


def photo_tubes(screen: Screen, tubes: list[dict | None] | None) -> set[int]:
    """Indices of the tubes that show a photo: a strip screen with a photo source
    (all six tubes), or an image tube with a camera:/entity_picture:/http source.
    Those tubes may be uploaded as JPEG when the clock supports it."""
    if screen.strip is not None:
        return set(range(6)) if is_photo_source(screen.strip.get("source")) else set()
    out: set[int] = set()
    for i, w in enumerate(tubes or []):
        if isinstance(w, dict) and w.get("type") == "image" and is_photo_source(w.get("source")):
            out.add(i)
    return out


# ---------------------------------------------------------------------------
# animators (animate.py): the parts of a tube that move between full renders
# ---------------------------------------------------------------------------

import animate as _animate  # noqa: E402  (registers below; animate.py imports nothing from here)


def _bg_hex(bg) -> str | None:
    if bg is None:
        return None
    return "#%02X%02X%02X" % tuple(max(0, min(255, int(c))) for c in tuple(bg)[:3])


class _TickerAnimator:
    """Scrolls the ticker band about 10 times a second (a 135 x (size+12) band is ~11 KB,
    ~53 ms to upload raw). Not animated when the text fits the tube."""

    FRAME = 0.1

    def interval(self, w: dict) -> float:
        return self.FRAME if ticker_cycle(w, w.get("font") if isinstance(w.get("font"), str) else None) > 0 else 0.0

    async def frame(self, w: dict, ctx, t: float, bg):
        w = dict(w)
        if w.get("background") is None and bg is not None:
            w["background"] = _bg_hex(bg)
        speed = _num(w.get("speed"), 40.0)
        fam = w.get("font") if isinstance(w.get("font"), str) else None
        if fam is None and ctx is not None:
            fam = ctx.default_font
        return ticker_band(w, t * speed, fam)


class _AnalogClockAnimator:
    """Redraws the clock face once a second when it has a seconds hand, else twice a
    minute (one full tube upload, ~218 ms raw)."""

    def interval(self, w: dict) -> float:
        seconds = w.get("seconds", True)
        if isinstance(seconds, str):
            seconds = seconds.strip().lower() not in ("false", "0", "no", "off")
        return 1.0 if seconds else 30.0

    async def frame(self, w: dict, ctx, t: float, bg):
        return draw_analog_clock(w, ctx, bg if bg is not None else (0, 0, 0)), (0, 0, TUBE_W, TUBE_H)


_animate.register("ticker", _TickerAnimator())
_animate.register("analog_clock", _AnalogClockAnimator())
