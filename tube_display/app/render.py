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

from PIL import Image, ImageDraw, ImageFont, ImageOps

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
        # The editor's designer sets this to a dict to get, per custom widget key, the
        # bounding box of each item it drew
        self.record_boxes: dict[str, list] | None = None
        # (path, mtime) -> decoded picture, so a big photo isn't decoded on every refresh
        self.image_cache: dict[tuple, Image.Image] = {}

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
          start: int, bold: bool = False):
    """The font at a fixed `size`, or the largest one <= start that fits the box."""
    if size:
        return fonts.load_font(size, bold=bold)
    return _fit_font(draw, text, max_w, max_h, start=start, bold=bold)


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
    size = _size(w.get("size"))
    pad = 8
    lines = text.split("\n")
    if size is None:
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
        font = fonts.load_font(size, bold=bold)
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
                  default_color) -> int:
    """Draw a widget's label at the bottom of the tube. A one-line label sits where it always
    did; one with several lines is fitted into the bottom of the tube and grows upwards.
    Returns how many more pixels than a one-line label it took, so the caller can make room."""
    label = _str(w.get("label"))
    if not label:
        return 0
    color = parse_color(w.get("label_color"), default_color)
    size = _size(w.get("label_size"))
    if "\n" not in label:
        _draw_centered(draw, TUBE_W // 2, legacy_cy, label, fonts.load_font(size or default_size), color)
        return 0
    font = _font(draw, label, size, TUBE_W - 8, 100, start=default_size)
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
    extra = _bottom_label(draw, w, 18, TUBE_H - 34, color)
    up = extra // 2      # a tall label pushes the number up and leaves it less room
    cx = TUBE_W // 2
    if unit and w.get("unit_newline"):
        # The value alone gets the full width; the unit goes on its own line below it
        font = _font(draw, value, vsize, TUBE_W - 8, TUBE_H - 110 - extra, start=140, bold=True)
        _draw_centered(draw, cx, TUBE_H // 2 - 34 - up, value, font, vcolor)
        ufont = _font(draw, unit, usize, TUBE_W - 12, 40, start=36, bold=True)
        _draw_centered(draw, cx, TUBE_H // 2 + 46 - extra, unit, ufont, ucolor)
    elif not unit and not label:
        # Just a value (e.g. one digit of a time): as big as the tube allows
        font = _font(draw, value, vsize, TUBE_W - 6, TUBE_H - 24, start=240, bold=True)
        _draw_centered(draw, cx, TUBE_H // 2, value, font, vcolor)
    else:
        main = value + unit
        font = _font(draw, main, vsize, TUBE_W - 12, TUBE_H - 70 - extra, start=110, bold=True)
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
        vfont = _font(draw, value, vsize, inner + 2, 44, start=48, bold=True)
        _draw_centered(draw, cx, cy - 8, value, vfont, vcolor)
        ufont = _font(draw, unit, usize, inner - 16, 20, start=20, bold=True)
        _draw_centered(draw, cx, cy + 24, unit, ufont, ucolor)
    else:
        vfont = _font(draw, value + unit, vsize, inner + 16, 40, start=34, bold=True)
        _draw_centered(draw, cx, cy, value + unit, vfont, vcolor)
    if label:
        if "\n" in label:
            top, bottom = cy + r + 8, TUBE_H - 8
            lfont = _font(draw, label, lsize, TUBE_W - 8, bottom - top, start=22)
            _draw_centered(draw, cx, (top + bottom) / 2, label, lfont, lcolor)
        else:
            _draw_centered(draw, cx, cy + r + 18, label, fonts.load_font(lsize or 16), lcolor)
    return img


def draw_bar(w: dict, ctx: RenderContext, bg) -> Image.Image:
    img = Image.new("RGB", (TUBE_W, TUBE_H), bg)
    draw = ImageDraw.Draw(img)
    color = parse_color(w.get("color"), CLAUDE_ORANGE)
    frac = _gauge_fraction(w)
    extra = _bottom_label(draw, w, 16, TUBE_H - 30, DEFAULT_COLOR)
    x0, x1 = 30, TUBE_W - 30
    y0, y1 = 24, TUBE_H - 56 - extra
    draw.rectangle([x0, y0, x1, y1], outline=TRACK_COLOR, width=6)
    fill_h = int((y1 - y0) * frac)
    if fill_h > 0:
        draw.rectangle([x0, y1 - fill_h, x1, y1], fill=color)
    value = _str(w.get("value")) + _str(w.get("unit"))
    vfont = _font(draw, value, _size(w.get("value_size")), TUBE_W - 16, 40, start=30, bold=True)
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
    # With text below, the icon sits in the upper part; on its own it is centred and a bit bigger
    cy = 88 if text else TUBE_H // 2
    size = _size(w.get("icon_size")) or (96 if text else 120)
    _draw_mdi(draw, TUBE_W // 2, cy, _str(w.get("icon")), size, color)
    if text:
        tfont = _font(draw, text, _size(w.get("text_size")), TUBE_W - 12, 60, start=24)
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


# ---------------------------------------------------------------------------
# custom widget: a list of freely placed items
# ---------------------------------------------------------------------------

ITEM_TYPES = ("text", "ring", "bar", "icon", "image", "rect")
Box = tuple[int, int, int, int]


def _item_text(img: Image.Image, draw: ImageDraw.ImageDraw, it: dict) -> Box:
    text = _str(it.get("text"))
    x, y = _num(it.get("x"), TUBE_W / 2), _num(it.get("y"), TUBE_H / 2)
    align = str(it.get("align") or "center").lower()
    bold = str(it.get("weight") or "").lower() == "bold"
    max_w = int(_num(it.get("width"), TUBE_W - 8))
    max_h = int(_num(it.get("height"), 60))
    font = _font(draw, text, _size(it.get("size")), max_w, max_h, start=200, bold=bold)
    if not text:
        return (int(x) - 6, int(y) - 6, int(x) + 6, int(y) + 6)
    return _draw_centered(draw, x, y, text, font, parse_color(it.get("color"), DEFAULT_COLOR), align)


def _no_track(track: Any) -> bool:
    """track: none hides the unfilled part of a ring or bar."""
    return isinstance(track, str) and track.strip().lower() == "none"


def _item_ring(img: Image.Image, draw: ImageDraw.ImageDraw, it: dict) -> Box:
    x, y = _num(it.get("x"), TUBE_W / 2), _num(it.get("y"), TUBE_H / 2)
    r = max(4.0, _num(it.get("radius"), 50))
    width = max(1.0, min(r, _num(it.get("thickness"), 10)))
    track = it.get("track")
    track_color = None if _no_track(track) else parse_color(track, TRACK_COLOR)
    return _draw_arc(img, x, y, r, width, _gauge_fraction(it),
                     parse_color(it.get("color"), CLAUDE_ORANGE), track_color,
                     start=_num(it.get("start"), -90), sweep=_num(it.get("sweep"), 360))


def _item_bar(img: Image.Image, draw: ImageDraw.ImageDraw, it: dict) -> Box:
    x, y = _num(it.get("x"), TUBE_W / 2), _num(it.get("y"), TUBE_H / 2)
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


def _item_icon(img: Image.Image, draw: ImageDraw.ImageDraw, it: dict) -> Box:
    x, y = _num(it.get("x"), TUBE_W / 2), _num(it.get("y"), TUBE_H / 2)
    return _draw_mdi(draw, x, y, _str(it.get("icon")), _size(it.get("size")) or 64,
                     parse_color(it.get("color"), CLAUDE_ORANGE))


def _item_rect(img: Image.Image, draw: ImageDraw.ImageDraw, it: dict) -> Box:
    x, y = _num(it.get("x"), TUBE_W / 2), _num(it.get("y"), TUBE_H / 2)
    bw, bh = max(1.0, _num(it.get("width"), 100)), max(1.0, _num(it.get("height"), 2))
    x0, y0, x1, y1 = round(x - bw / 2), round(y - bh / 2), round(x + bw / 2), round(y + bh / 2)
    radius = int(max(0, min(min(x1 - x0, y1 - y0) / 2, _num(it.get("radius"), 0))))
    draw.rounded_rectangle([x0, y0, x1 - 1, y1 - 1], radius=radius,
                           fill=parse_color(it.get("color"), DEFAULT_COLOR))
    return (x0, y0, x1, y1)


async def _item_image(img: Image.Image, it: dict, ctx: RenderContext, bg) -> Box:
    x, y = _num(it.get("x"), TUBE_W / 2), _num(it.get("y"), TUBE_H / 2)
    bw = int(max(1, min(TUBE_W * 2, _num(it.get("width"), TUBE_W))))
    bh = int(max(1, min(TUBE_H * 2, _num(it.get("height"), TUBE_H))))
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


async def draw_custom(w: dict, ctx: RenderContext, bg) -> Image.Image:
    """Items drawn in order (later ones on top), each placed by its own x/y in tube pixels
    (0,0 = top left, the tube is 135 x 240). An item that fails shows a small red "!"."""
    bg = parse_color(w.get("background"), bg)
    img = Image.new("RGB", (TUBE_W, TUBE_H), bg)
    draw = ImageDraw.Draw(img)
    key = str(w.get("_key") or "custom")
    boxes: list[Box] = []
    items = w.get("items")
    for i, it in enumerate(items if isinstance(items, list) else []):
        try:
            if not isinstance(it, dict):
                raise ValueError("item must be a mapping")
            kind = it.get("type")
            if kind == "image":
                box = await _item_image(img, it, ctx, bg)
                draw = ImageDraw.Draw(img)
            elif kind in _ITEM_DRAW:
                box = _ITEM_DRAW[kind](img, draw, it)
            else:
                raise ValueError(f"unknown item type {kind!r} (expected one of {', '.join(ITEM_TYPES)})")
        except Exception as err:
            ctx.note_error(f"{key} item {i}", str(err))
            x = _num(it.get("x"), TUBE_W / 2) if isinstance(it, dict) else TUBE_W / 2
            y = _num(it.get("y"), TUBE_H / 2) if isinstance(it, dict) else TUBE_H / 2
            box = _draw_centered(draw, x, y, "!", fonts.load_font(24, bold=True), (255, 80, 80))
        boxes.append(tuple(int(v) for v in box))
    if ctx.record_boxes is not None:
        ctx.record_boxes[key] = boxes
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
    "custom": draw_custom,
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
