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


# ----------------------------------------------------------------------
# picture placement, multi-line labels, sizes, custom tubes
# ----------------------------------------------------------------------

def _red_box(img):
    """Bounding box of the pure-red pixels."""
    pts = [(x, y) for y in range(img.height) for x in range(img.width) if img.getpixel((x, y)) == (255, 0, 0)]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return (min(xs), min(ys), max(xs) + 1, max(ys) + 1) if pts else None


def test_fit_image_contain_shows_whole_picture_on_background():
    src = Image.new("RGB", (100, 100), (255, 0, 0))
    img = render._fit_image(src, TUBE_W, TUBE_H, "contain", bg=(0, 0, 255))
    assert img.size == (TUBE_W, TUBE_H)
    # a square in a tall tube: full width, centred, background above and below
    x0, y0, x1, y1 = _red_box(img)
    assert (x0, x1) == (0, TUBE_W) and abs(y0 - 52.5) <= 1.5 and abs(y1 - 187.5) <= 1.5
    assert img.getpixel((5, 5)) == (0, 0, 255)


def test_fit_image_zoom_and_offset_move_the_picture():
    src = Image.new("RGB", (100, 100), (255, 0, 0))
    img = render._fit_image(src, TUBE_W, TUBE_H, "contain", zoom=0.4, offset_x=20, offset_y=-60, bg=(0, 0, 0))
    x0, y0, x1, y1 = _red_box(img)
    assert (x1 - x0, y1 - y0) == (54, 54)                       # 135 * 0.4
    assert abs((x0 + x1) / 2 - (TUBE_W / 2 + 20)) <= 1
    assert abs((y0 + y1) / 2 - (TUBE_H / 2 - 60)) <= 1
    # moved right off the tube: just the background, no error
    gone = render._fit_image(src, TUBE_W, TUBE_H, "contain", offset_x=500, bg=(1, 2, 3))
    assert _red_box(gone) is None and gone.getpixel((60, 100)) == (1, 2, 3)


def test_fit_image_cover_unchanged_and_rotate():
    src = Image.new("RGB", (200, 100), (255, 0, 0))
    assert _red_box(render._fit_image(src, TUBE_W, TUBE_H, "cover")) == (0, 0, TUBE_W, TUBE_H)
    # wide picture turned a quarter: it is tall now, so contain uses more of the tube
    flat = _red_box(render._fit_image(src, TUBE_W, TUBE_H, "contain"))
    turned = _red_box(render._fit_image(src, TUBE_W, TUBE_H, "contain", rotate=90))
    assert flat[3] - flat[1] < 80 and turned[3] - turned[1] > 200


def test_image_widget_background_and_placement_fields(tmp_path):
    Image.new("RGB", (50, 50), (255, 0, 0)).save(tmp_path / "sq.png")
    ctx = _ctx()
    ctx.images_dir = str(tmp_path)
    w = {"type": "image", "source": "file:sq.png", "fit": "contain", "zoom": 0.5, "offset_y": 50,
         "background": "#00FF00"}
    img = asyncio.run(render.draw_image(w, ctx, (0, 0, 0)))
    assert img.getpixel((2, 2)) == (0, 255, 0)                 # the widget's own background
    x0, y0, x1, y1 = _red_box(img)
    assert abs((y0 + y1) / 2 - (TUBE_H / 2 + 50)) <= 1
    # the decoded file is kept, and dropped again when the file changes
    assert len(ctx.image_cache) == 1


def _ink_rows(img, bg=(0, 0, 0)):
    return [y for y in range(img.height) if any(img.getpixel((x, y)) != bg for x in range(img.width))]


def test_ring_multiline_label_moves_ring_up():
    one = _render_widget({"type": "ring", "value": "40", "label": "Week"})
    many = _render_widget({"type": "ring", "value": "40", "label": "under pace\nResets\nThu 10:00"})
    assert min(_ink_rows(many)) < min(_ink_rows(one))          # ring higher
    assert max(_ink_rows(many)) > max(_ink_rows(one))          # label uses the space below
    assert max(_ink_rows(many)) < TUBE_H


def test_typed_backslash_n_is_a_line_break():
    a = _render_widget({"type": "big_number", "value": "1", "label": "two\\nlines"})
    b = _render_widget({"type": "big_number", "value": "1", "label": "two\nlines"})
    assert a.tobytes() == b.tobytes()


def test_explicit_sizes_change_the_text():
    auto = _render_widget({"type": "big_number", "value": "8"})
    small = _render_widget({"type": "big_number", "value": "8", "value_size": 20})
    assert len(_ink_rows(small)) < len(_ink_rows(auto)) / 2
    thin = _render_widget({"type": "ring", "value": "50", "thickness": 3})
    thick = _render_widget({"type": "ring", "value": "50", "thickness": 20})
    def count(im):
        return sum(1 for x in range(im.width) for y in range(im.height) if im.getpixel((x, y)) != (0, 0, 0))

    assert count(thin) < count(thick)


def test_custom_widget_items_and_boxes():
    ctx = _ctx()
    ctx.record_boxes = {}
    w = {"type": "custom", "_key": "s:tube0", "items": [
        {"type": "ring", "x": 67, "y": 60, "radius": 50, "thickness": 10, "value": "50", "color": "#FF0000"},
        {"type": "text", "x": 67, "y": 60, "text": "50%", "size": 24, "weight": "bold"},
        {"type": "text", "x": 10, "y": 150, "text": "left\ntwo", "size": 16, "align": "left"},
        {"type": "bar", "x": 67, "y": 200, "width": 100, "height": 10, "value": "25", "color": "#00FF00"},
        {"type": "rect", "x": 67, "y": 220, "width": 80, "height": 2, "color": "#0000FF"},
        {"type": "icon", "x": 67, "y": 232, "size": 12, "icon": "mdi:robot"},
        {"type": "image", "x": 20, "y": 20, "width": 30, "height": 30, "source": "builtin:claude"},
    ]}
    img = asyncio.run(render.draw_custom(w, ctx, (0, 0, 0)))
    assert img.size == (TUBE_W, TUBE_H)
    boxes = ctx.record_boxes["s:tube0"]
    assert len(boxes) == 7
    assert boxes[0] == (17, 10, 117, 110)                      # ring: centre +- radius
    assert boxes[2][0] in (9, 10, 11)                          # left-aligned text starts at x
    assert boxes[3] == (17, 195, 117, 205)
    assert img.getpixel((22, 60)) == (60, 60, 60)              # unfilled (left) half: track
    assert img.getpixel((113, 60))[0] > 200                    # filled half (clockwise from the top)
    assert img.getpixel((30, 200)) == (0, 255, 0)              # bar: first quarter filled
    assert img.getpixel((100, 200)) == (60, 60, 60)            # the rest is track
    assert img.getpixel((67, 220)) == (0, 0, 255)
    assert ctx.last_errors == {}


def test_custom_widget_bad_item_shows_mark_but_keeps_the_rest():
    ctx = _ctx()
    w = {"type": "custom", "items": [
        {"type": "rect", "x": 67, "y": 20, "width": 50, "height": 10, "color": "#0000FF"},
        {"type": "image", "x": 67, "y": 120, "source": "file:nope.png"},
    ]}
    img = asyncio.run(render.draw_custom(w, ctx, (0, 0, 0)))
    assert img.getpixel((67, 20)) == (0, 0, 255)
    assert any("item 1" in k for k in ctx.last_errors)


def test_templates_anywhere_in_a_widget_are_resolved():
    async def renderer(t):
        return {"{{ a }}": "75", "{{ lbl }}": "from\ntemplate", "{% if x %}#00FF00{% endif %}": "#00FF00"}[t]

    tubes = [{"type": "blank"}] * 4 + [
        {"type": "ring", "value": "{{ a }}", "label": "{{ lbl }}", "label_color": "{% if x %}#00FF00{% endif %}"},
        {"type": "custom", "items": [{"type": "bar", "x": 67, "y": 100, "width": 100, "height": 20,
                                      "value": "{{ a }}", "color": "{% if x %}#00FF00{% endif %}"}]},
    ]
    screen = Screen(name="t", tubes=tubes)
    assert set(render.collect_templates(screen)) == {"{{ a }}", "{{ lbl }}", "{% if x %}#00FF00{% endif %}"}
    ctx = _ctx(renderer)
    strip = asyncio.run(render.render_screen(screen, ctx))
    assert ctx.last_errors == {}
    assert strip.getpixel((5 * TUBE_W + 40, 100)) == (0, 255, 0)      # bar filled to 75%
    assert strip.getpixel((5 * TUBE_W + 110, 100)) == (60, 60, 60)
    assert (0, 255, 0) in [strip.getpixel((4 * TUBE_W + x, y)) for x in range(TUBE_W) for y in range(130, TUBE_H)]


def test_failed_template_inside_custom_item_marks_the_tube():
    async def renderer(t):
        raise RuntimeError("boom")

    tubes = [{"type": "blank"}] * 5 + [{"type": "custom", "items": [{"type": "text", "text": "{{ bad }}"}]}]
    ctx = _ctx(renderer)
    asyncio.run(render.render_screen(Screen(name="t", tubes=tubes), ctx))
    assert "items[0].text" in ctx.last_errors["t:tube5"]


def test_strip_placement(tmp_path):
    Image.new("RGB", (100, 100), (255, 0, 0)).save(tmp_path / "sq.png")
    ctx = _ctx()
    ctx.images_dir = str(tmp_path)
    screen = Screen(name="s", background="#000000",
                    strip={"source": "file:sq.png", "fit": "contain", "offset_x": -200, "background": "#000080"})
    strip = asyncio.run(render.render_screen(screen, ctx))
    x0, y0, x1, y1 = _red_box(strip)
    assert (y0, y1) == (0, STRIP_H) and abs((x0 + x1) / 2 - (STRIP_W / 2 - 200)) <= 1
    assert strip.getpixel((STRIP_W - 5, 5)) == (0, 0, 128)
