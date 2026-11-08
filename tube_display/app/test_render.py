"""Widget rendering: every widget draws a 135x240 RGB image without error.

Fonts may be missing in the test environment -> Pillow's default font is
used. MDI icon tests are skipped when the MDI font is absent (no network).
"""

import asyncio

import pytest

from conftest import pil, requires_pil

pytestmark = requires_pil

if pil:
    from PIL import Image

    import fonts
    import render
    from rgb565 import STRIP_W, STRIP_H, TUBE_H, TUBE_W
    from screens import Screen


def _ctx(renderer=None):
    async def default_renderer(t):
        return t  # no templates in these widgets
    return render.RenderContext(renderer or default_renderer)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) \
        if not asyncio.iscoroutinefunction(coro) else asyncio.run(coro)


def _render_widget(w, bg=(0, 0, 0)):
    ctx = _ctx()
    fn = render._WIDGET_DRAW.get(w["type"]) or render._WIDGET_DRAW_ASYNC.get(w["type"])
    assert fn is not None, f"no draw function for {w['type']}"
    result = fn(w, ctx, bg)
    if asyncio.iscoroutine(result):
        result = asyncio.run(result)
    return result


WIDGETS = [
    {"type": "text", "text": "Hello\nWorld", "size": "auto", "color": "#FFFFFF", "align": "center"},
    {"type": "text", "text": "left aligned", "align": "left", "weight": "bold", "size": 20},
    {"type": "big_number", "value": "42", "unit": "%", "label": "Pace"},
    {"type": "ring", "value": "63", "max": 100, "label": "Session", "unit": "%", "color": "#D97757"},
    {"type": "ring", "value": "150", "max": 100, "label": "over"},  # clamped
    {"type": "bar", "value": "0.5", "max": 1, "label": "Load", "unit": " kW"},
    {"type": "icon", "icon": "mdi:robot", "text": "online", "color": "#D97757"},
    {"type": "image", "source": "builtin:claude"},
    {"type": "blank"},
]


@pytest.mark.parametrize("w", WIDGETS, ids=[w["type"] + str(i) for i, w in enumerate(WIDGETS)])
def test_widget_renders_135x240_rgb(w):
    img = _render_widget(w)
    assert isinstance(img, Image.Image)
    assert img.size == (TUBE_W, TUBE_H)
    assert img.mode == "RGB"


def test_sparkline_with_fake_ha():
    class FakeHa:
        async def history(self, entity, hours):
            return [{"state": str(v)} for v in (1, 5, 3, 9, 4)]
    ctx = render.RenderContext(lambda t: asyncio.sleep(0, result=t), ha=FakeHa())
    img = asyncio.run(render.draw_sparkline(
        {"type": "sparkline", "entity": "sensor.temp", "hours": 24, "unit": "°"}, ctx, (0, 0, 0)))
    assert img.size == (TUBE_W, TUBE_H)
    assert img.mode == "RGB"


def test_sparkline_no_data():
    class FakeHa:
        async def history(self, entity, hours):
            return []
    ctx = render.RenderContext(lambda t: asyncio.sleep(0, result=t), ha=FakeHa())
    img = asyncio.run(render.draw_sparkline(
        {"type": "sparkline", "entity": "sensor.x", "hours": 1}, ctx, (0, 0, 0)))
    assert img.size == (TUBE_W, TUBE_H)


def test_image_http_url_with_fake_session():
    import io
    png = io.BytesIO()
    Image.new("RGB", (300, 100), (10, 200, 30)).save(png, format="PNG")
    png_bytes = png.getvalue()

    class FakeResp:
        status = 200
        async def read(self):
            return png_bytes
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False

    class FakeSession:
        def get(self, url, timeout=None):
            return FakeResp()

    ctx = render.RenderContext(lambda t: asyncio.sleep(0, result=t), session=FakeSession())
    img = asyncio.run(render.draw_image(
        {"type": "image", "source": "http://example.test/pic.png", "fit": "cover"}, ctx, (0, 0, 0)))
    assert img.size == (TUBE_W, TUBE_H)


def test_builtin_claude_is_orange_starburst():
    img = render.draw_builtin_claude((0, 0, 0))
    assert img.size == (TUBE_W, TUBE_H)
    # centre should be orange-ish, corners black
    r, g, b = img.getpixel((TUBE_W // 2, TUBE_H // 2))
    assert r > 150 and 80 < g < 160 and b < 130
    assert img.getpixel((2, 2)) == (0, 0, 0)


def test_full_screen_renders_810x240():
    screen = Screen(
        name="s",
        tubes=[
            {"type": "image", "source": "builtin:claude"},
            {"type": "ring", "value": "{{ v }}", "max": 100, "label": "S"},
            {"type": "text", "text": "{{ t }}", "size": "auto"},
            {"type": "big_number", "value": "7", "unit": "%", "label": "P"},
            {"type": "bar", "value": "0.3", "max": 1},
            {"type": "blank"},
        ],
    )

    async def renderer(t):
        return {"{{ v }}": "55", "{{ t }}": "hello"}.get(t, t)

    ctx = render.RenderContext(renderer)
    strip = asyncio.run(render.render_screen(screen, ctx))
    assert strip.size == (STRIP_W, STRIP_H)
    assert strip.mode == "RGB"


def test_template_failure_shows_bang_not_crash():
    screen = Screen(name="s", tubes=[{"type": "text", "text": "{{ boom }}"}] * 6)

    async def bad_renderer(t):
        raise RuntimeError("template exploded")

    ctx = render.RenderContext(bad_renderer)
    strip = asyncio.run(render.render_screen(screen, ctx))
    assert strip.size == (STRIP_W, STRIP_H)
    # error tile: red "!" somewhere in tube 0
    tile = strip.crop((0, 0, TUBE_W, TUBE_H))
    colors = tile.getcolors(maxcolors=100000)
    assert any(c > 0 and rgb[0] > 200 and rgb[1] < 120 for c, rgb in colors)


def test_error_logged_once_per_change():
    ctx = render.RenderContext(lambda t: asyncio.sleep(0, result=t))
    ctx.note_error("k", "same error")
    ctx.note_error("k", "same error")  # no new log
    assert ctx.last_errors["k"] == "same error"
    ctx.note_error("k", "different")
    assert ctx.last_errors["k"] == "different"


@pytest.mark.skipif(not pil or not fonts.mdi_available(),
                    reason="MDI font not installed (no network in tests)")
def test_mdi_icon_renders_glyph():
    cp = fonts.mdi_codepoint("robot")
    assert cp is not None
    img = _render_widget({"type": "icon", "icon": "mdi:robot", "text": "bot"})
    assert img.size == (TUBE_W, TUBE_H)


def test_parse_color():
    assert render.parse_color("#D97757") == (0xD9, 0x77, 0x57)
    assert render.parse_color("#fff") == (255, 255, 255)
    assert render.parse_color("not a color") == (255, 255, 255)
    assert render.parse_color(None, (1, 2, 3)) == (1, 2, 3)



def test_template_bytes_reply_is_decoded():
    import asyncio
    from ha import HaClient

    class Fake(HaClient):
        def __init__(self):
            pass

        async def _request(self, method, path, **kwargs):
            return b"42\nunder"

    assert asyncio.run(Fake().render_template("x")) == "42\nunder"


def test_file_image_source_with_transparency(tmp_path):
    import asyncio
    import render
    from PIL import Image
    Image.new("RGBA", (10, 10), (0, 0, 0, 0)).save(tmp_path / "clear.png")
    ctx = render.RenderContext(lambda t: asyncio.sleep(0, result=t))
    ctx.images_dir = str(tmp_path)
    img = asyncio.run(render.draw_image({"type": "image", "source": "file:clear.png"}, ctx, (0, 0, 255)))
    assert img.size == (render.TUBE_W, render.TUBE_H)
    assert img.getpixel((60, 100)) == (0, 0, 255)   # transparent -> screen background
    try:
        asyncio.run(render.draw_image({"type": "image", "source": "file:../x.png"}, ctx, (0, 0, 0)))
        assert False, "path outside the images folder must fail"
    except ValueError:
        pass


def test_icon_without_text_is_centred():
    import asyncio
    import render
    ctx = render.RenderContext(lambda t: asyncio.sleep(0, result=t))
    img = render.draw_icon({"type": "icon", "icon": "mdi:no-such-icon", "color": "#FFFFFF"}, ctx, (0, 0, 0))
    ys = [y for y in range(render.TUBE_H) for x in range(render.TUBE_W) if img.getpixel((x, y)) != (0, 0, 0)]
    assert abs((min(ys) + max(ys)) / 2 - render.TUBE_H / 2) <= 2
