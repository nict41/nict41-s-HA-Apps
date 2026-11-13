"""Widget rendering: every widget draws a 135x240 RGB image without error.

Fonts may be missing in the test environment -> Pillow's default font is
used. MDI icon tests are skipped when the MDI font is absent (no network).
"""

import asyncio
import io

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


def test_ha_state_and_forecast_daily():
    import asyncio
    from ha import HaClient, HaError

    class Fake(HaClient):
        def __init__(self, replies):
            self.replies = replies
            self.calls = []

        async def _request(self, method, path, **kwargs):
            self.calls.append((method, path, kwargs))
            r = self.replies.get((method, path))
            if isinstance(r, Exception):
                raise r
            return r

    async def main():
        # state(): the full state object, None on a 404-ish HaError
        ha = Fake({("GET", "/states/weather.home"): {"state": "sunny", "attributes": {"temperature": 21}}})
        assert await ha.state("weather.home") == {"state": "sunny", "attributes": {"temperature": 21}}
        ha404 = Fake({("GET", "/states/weather.gone"): HaError("404")})
        assert await ha404.state("weather.gone") is None

        # forecast_daily(): the wrapped service_response shape
        wrapped = {"service_response": {"weather.home": {"forecast": [
            {"datetime": "2026-11-12T00:00:00+00:00", "temperature": 14, "templow": 6}]}}}
        ha2 = Fake({("POST", "/services/weather/get_forecasts"): wrapped})
        fc = await ha2.forecast_daily("weather.home")
        assert fc == [{"datetime": "2026-11-12T00:00:00+00:00", "temperature": 14, "templow": 6}]
        method, path, kwargs = ha2.calls[0]
        assert method == "POST" and path == "/services/weather/get_forecasts"
        assert kwargs["json"] == {"entity_id": "weather.home", "type": "daily"}
        assert kwargs["params"] == {"return_response": "true"}
        # the bare {"<entity>": {...}} shape is accepted too
        ha3 = Fake({("POST", "/services/weather/get_forecasts"): {"weather.home": {"forecast": [{"temperature": 1}]}}})
        assert await ha3.forecast_daily("weather.home") == [{"temperature": 1}]
        # a failing call -> [] (the widget then just shows no high/low)
        ha4 = Fake({("POST", "/services/weather/get_forecasts"): HaError("500")})
        assert await ha4.forecast_daily("weather.home") == []

    asyncio.run(main())


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


def _non_bg_pixels(img, bg):
    return [(x, y) for y in range(img.height) for x in range(img.width)
            if img.getpixel((x, y)) != bg]


def _dominant_ink(img, bg):
    """The most common non-background colour (anti-aliasing blends the edges)."""
    from collections import Counter
    counts = Counter(img.getpixel((x, y)) for y in range(img.height) for x in range(img.width))
    counts.pop(bg, None)
    return counts.most_common(1)[0][0]


def test_render_message_basic_shape_and_color():
    ctx = _ctx()
    img = render.render_message("Hello there", ctx)
    assert img.size == (STRIP_W, STRIP_H) and img.mode == "RGB"
    # default: white text on black, spanning the full strip (no icon)
    px = _non_bg_pixels(img, (0, 0, 0))
    assert px, "message text must be drawn"
    assert _dominant_ink(img, (0, 0, 0)) == (255, 255, 255)
    xs = [p[0] for p in px]
    assert min(xs) < STRIP_W // 2 < max(xs)          # centred across the strip


def test_render_message_color_and_background():
    ctx = _ctx()
    img = render.render_message("Hi", ctx, color="#FF0000", background="#0000FF")
    px = _non_bg_pixels(img, (0, 0, 255))
    assert px
    assert _dominant_ink(img, (0, 0, 255)) == (255, 0, 0)


def test_render_message_icon_uses_tube0_only_for_icon():
    ctx = _ctx()
    img = render.render_message("Doorbell!", ctx, icon="mdi:bell")
    # the icon area (tube 0) has ink, and the text area starts after x=135
    icon_px = _non_bg_pixels(img.crop((0, 0, TUBE_W, TUBE_H)), (0, 0, 0))
    assert icon_px, "icon must be drawn on tube 0"
    text_px = _non_bg_pixels(img.crop((TUBE_W, 0, STRIP_W, STRIP_H)), (0, 0, 0))
    assert text_px, "text must be drawn right of tube 0"
    xs = [p[0] for p in text_px]
    assert min(xs) >= 0 and max(xs) < STRIP_W - TUBE_W


def test_render_message_long_text_wraps_to_at_most_3_lines():
    ctx = _ctx()
    long_text = " ".join(f"word{i}" for i in range(60))
    img = render.render_message(long_text, ctx)
    assert img.size == (STRIP_W, STRIP_H)
    # ink must stay inside the 8 px margins
    px = _non_bg_pixels(img, (0, 0, 0))
    assert px
    assert min(p[0] for p in px) >= 8 - 2 and max(p[0] for p in px) <= STRIP_W - 8 + 2
    assert min(p[1] for p in px) >= 8 - 2 and max(p[1] for p in px) <= STRIP_H - 8 + 2


# ----------------------------------------------------------------------
# the five new widget types: graph, progress, analog_clock, weather, ticker
# ----------------------------------------------------------------------

class _FakeHa:
    """history + state + forecast for the graph/weather tests."""

    def __init__(self, states=None, weather=None, forecast=None):
        self._states = states if states is not None else [{"state": str(v)} for v in (1, 5, 3, 9, 4, "unavailable", 7)]
        self._weather = weather
        self._forecast = forecast if forecast is not None else []
        self.forecast_calls = 0

    async def history(self, entity, hours):
        return self._states

    async def state(self, entity):
        return self._weather

    async def forecast_daily(self, entity):
        self.forecast_calls += 1
        return self._forecast


def _ctx_ha(ha):
    return render.RenderContext(lambda t: asyncio.sleep(0, result=t), ha=ha)


def test_new_widget_types_are_registered():
    for t in ("graph", "progress", "analog_clock", "weather", "ticker"):
        assert t in render._WIDGET_DRAW or t in render._WIDGET_DRAW_ASYNC, t
        assert t in render._WIDGET_TEMPLATE_FIELDS, t


def test_widget_template_fields_cover_every_type():
    # the editor mirrors this table; every widget type must have an entry
    from screens import WIDGET_TYPES
    assert set(render._WIDGET_TEMPLATE_FIELDS) == set(WIDGET_TYPES)


def test_graph_renders_all_styles_and_skips_non_numeric():
    ha = _FakeHa()
    ctx = _ctx_ha(ha)
    for style in ("line", "area", "bars"):
        w = {"type": "graph", "entity": "sensor.temp", "hours": 24, "style": style,
             "label": "Temp", "unit": "°", "color": "#D97757"}
        img = asyncio.run(render.draw_graph(w, ctx, (0, 0, 0)))
        assert img.size == (TUBE_W, TUBE_H) and img.mode == "RGB"
        assert _non_bg_pixels(img, (0, 0, 0)), f"{style} must draw something"


def test_graph_no_data_and_no_ha():
    ctx = _ctx_ha(_FakeHa(states=[]))
    img = asyncio.run(render.draw_graph({"type": "graph", "entity": "sensor.x"}, ctx, (0, 0, 0)))
    assert img.size == (TUBE_W, TUBE_H)
    assert _non_bg_pixels(img, (0, 0, 0))          # "no data"
    # no HA client at all: raises, render_screen turns that into the "!" tile
    with pytest.raises(ValueError):
        asyncio.run(render.draw_graph({"type": "graph", "entity": "x"}, _ctx(), (0, 0, 0)))


def test_graph_fixed_axis_and_resample():
    values = list(range(500))                       # far more than the 120 point cap
    ha = _FakeHa(states=[{"state": str(v)} for v in values])
    ctx = _ctx_ha(ha)
    w = {"type": "graph", "entity": "sensor.x", "style": "line", "min": 0, "max": 600,
         "points": 120, "show_value": False}
    img = asyncio.run(render.draw_graph(w, ctx, (0, 0, 0)))
    assert img.size == (TUBE_W, TUBE_H)
    rs = render._resample(values, 120)
    assert len(rs) == 120                            # capped at `points`
    assert abs(sum(rs) / len(rs) - sum(values) / len(values)) < 1.0   # bucket means keep the average
    assert render._resample([1, 2, 3], 120) == [1, 2, 3]   # fewer points than the cap: unchanged


def test_graph_area_fill_is_35_percent_blend():
    ha = _FakeHa(states=[{"state": str(v)} for v in (0, 10, 0)])
    ctx = _ctx_ha(ha)
    w = {"type": "graph", "entity": "sensor.x", "style": "area", "color": "#FF0000", "show_value": False}
    img = asyncio.run(render.draw_graph(w, ctx, (0, 0, 0)))
    # somewhere inside the filled area the pixel is red blended 35% into black
    assert (89, 0, 0) in [img.getpixel((x, y)) for x in range(TUBE_W) for y in range(TUBE_H)]


def test_progress_renders_and_clamps():
    ctx = _ctx()
    for value in ("42", "150", "junk"):
        img = render.draw_progress({"type": "progress", "value": value, "max": 100,
                                    "label": "Print", "finish": "2999-01-01T00:00:00+00:00"}, ctx, (0, 0, 0))
        assert img.size == (TUBE_W, TUBE_H) and img.mode == "RGB"
        assert _non_bg_pixels(img, (0, 0, 0))


def test_format_eta_remaining():
    from datetime import datetime, timezone, timedelta
    now = datetime(2026, 11, 12, 12, 0, 0, tzinfo=timezone.utc)
    assert render.format_eta("2026-11-12T12:23:00+00:00", now, "remaining") == "23 min"
    assert render.format_eta("2026-11-12T13:05:00+00:00", now, "remaining") == "1 h 05"
    assert render.format_eta("2026-11-12T12:00:30+00:00", now, "remaining") == "<1 min"
    assert render.format_eta("2026-11-12T11:00:00Z", now, "remaining") == "done"     # past
    assert render.format_eta("2026-11-12T12:00:00+00:00", now, "remaining") == "done"  # <= 0
    assert render.format_eta("garbage", now, "remaining") == ""
    assert render.format_eta("", now, "remaining") == ""
    assert render.format_eta(None, now, "remaining") == ""


def test_format_eta_time_and_epoch_and_hhmm():
    from datetime import datetime, timezone, timedelta
    now = datetime(2026, 11, 12, 12, 0, 0, tzinfo=timezone.utc)
    assert render.format_eta("2026-11-12T14:23:00+00:00", now, "time") == "14:23"
    assert render.format_eta("2026-11-12T11:00:00Z", now, "time") == "done"
    epoch = int((now + timedelta(minutes=65)).timestamp())
    assert render.format_eta(str(epoch), now, "remaining") == "1 h 05"
    assert render.format_eta(f"{epoch}.5", now, "remaining") == "1 h 05"     # float epoch string
    assert render.format_eta("13:30", now, "time") == "13:30"                # later today
    # HH:MM already past today means tomorrow: still in the future, so "time" shows it
    assert render.format_eta("11:30", now, "time") == "11:30"
    assert render.format_eta("11:30", now, "remaining") == "23 h 30"
    # naive ISO is read as local time; compare against the same instant in UTC
    local_finish = datetime(2026, 11, 12, 12, 30, 0).astimezone(timezone.utc)
    assert render.format_eta("2026-11-12T12:30:00", now, "remaining") == \
        render.format_eta(local_finish.isoformat(), now, "remaining") == "30 min"
    # naive now + aware finish must not crash (draw_progress passes datetime.now())
    naive = datetime(2026, 11, 12, 12, 0, 0)
    assert render.format_eta("2026-11-12T14:23:00+00:00", naive, "remaining") == "2 h 23"
    assert render.format_eta("2026-11-12T12:30:00", naive, "time") == "12:30"


def test_hand_angles():
    from datetime import datetime
    h, m, s = render.hand_angles(datetime(2026, 11, 12, 0, 0, 0))
    assert (h, m, s) == (0.0, 0.0, 0.0)
    h, m, s = render.hand_angles(datetime(2026, 11, 12, 3, 30, 15))
    assert h == 105.125 and m == 181.5 and s == 90.0
    h, m, s = render.hand_angles(datetime(2026, 11, 12, 23, 59, 59))
    assert abs(h - (11 + 59 / 60 + 59 / 3600) * 30) < 1e-9
    assert abs(m - (59 + 59 / 60) * 6) < 1e-9 and abs(s - 59 * 6) < 1e-9


def test_analog_clock_renders_with_and_without_extras():
    ctx = _ctx()
    for w in ({"type": "analog_clock"},
              {"type": "analog_clock", "tz": "Europe/Berlin", "label": "Berlin", "numerals": True},
              {"type": "analog_clock", "seconds": False, "tz": "not/a-zone", "color": "#00FF00",
               "accent": "#FF0000", "face": "#101010"}):
        img = render.draw_analog_clock(w, ctx, (0, 0, 0))
        assert img.size == (TUBE_W, TUBE_H) and img.mode == "RGB"
        assert _non_bg_pixels(img, (0, 0, 0))


def test_weather_icon_map_covers_all_conditions():
    conditions = ("sunny", "cloudy", "partlycloudy", "rainy", "pouring", "snowy", "snowy-rainy",
                  "fog", "hail", "lightning", "lightning-rainy", "windy", "windy-variant",
                  "clear-night", "exceptional")
    for c in conditions:
        assert render.WEATHER_ICONS[c].startswith("mdi:weather-") or c == "exceptional"
    assert render.WEATHER_ICONS["exceptional"] == "mdi:alert-circle-outline"
    assert render.WEATHER_ICONS.get("made-up", render.WEATHER_ICONS["cloudy"]) == "mdi:weather-cloudy"


def test_weather_renders_today_and_now():
    from datetime import datetime
    today = datetime.now().date().isoformat()
    ha = _FakeHa(weather={"state": "partlycloudy",
                          "attributes": {"temperature": 12.3, "temperature_unit": "°C"}},
                 forecast=[{"datetime": today + "T00:00:00+00:00", "temperature": 14, "templow": 6}])
    ctx = _ctx_ha(ha)
    for show in ("today", "now"):
        img = asyncio.run(render.draw_weather(
            {"type": "weather", "entity": "weather.home", "show": show, "label": "Home"}, ctx, (0, 0, 0)))
        assert img.size == (TUBE_W, TUBE_H) and img.mode == "RGB"
        assert _non_bg_pixels(img, (0, 0, 0))
    # unknown condition falls back to the cloudy icon, missing temperature shows "--"
    ha2 = _FakeHa(weather={"state": "weird", "attributes": {}})
    img = asyncio.run(render.draw_weather({"type": "weather", "entity": "weather.x"}, _ctx_ha(ha2), (0, 0, 0)))
    assert img.size == (TUBE_W, TUBE_H)
    # entity gone -> "no data", no raise
    img = asyncio.run(render.draw_weather({"type": "weather", "entity": "weather.gone"},
                                          _ctx_ha(_FakeHa(weather=None)), (0, 0, 0)))
    assert img.size == (TUBE_W, TUBE_H) and _non_bg_pixels(img, (0, 0, 0))


def test_weather_forecast_cached_10_minutes():
    from datetime import datetime
    today = datetime.now().date().isoformat()
    ha = _FakeHa(weather={"state": "sunny", "attributes": {"temperature": 20, "temperature_unit": "°C"}},
                 forecast=[{"datetime": today + "T00:00:00+00:00", "temperature": 21, "templow": 9}])
    ctx = _ctx_ha(ha)
    ticks = iter([1000.0, 1000.0, 1000.0 + 599, 1000.0 + 600])   # injectable clock
    ctx.now_monotonic = lambda: next(ticks)
    for _ in range(3):
        asyncio.run(render.draw_weather({"type": "weather", "entity": "weather.home"}, ctx, (0, 0, 0)))
    assert ha.forecast_calls == 1                       # 2nd and 3rd call within 10 min: cache
    asyncio.run(render.draw_weather({"type": "weather", "entity": "weather.home"}, ctx, (0, 0, 0)))
    assert ha.forecast_calls == 2                       # after 10 min: fetched again


def test_ticker_band_and_cycle():
    long_text = "headline one • headline two • headline three • " * 3
    w = {"type": "ticker", "text": long_text, "size": 28, "y": 120, "speed": 40}
    band, box = render.ticker_band(w, 0)
    assert band.size == (TUBE_W, 28 + 12)
    assert box == (0, 120 - 20, TUBE_W, 40)          # (x, y, width, height) like every animator rect
    cycle = render.ticker_cycle(w)
    assert cycle > 0
    # one full loop of the offset must give the same band again (seamless loop)
    band2, _ = render.ticker_band(w, cycle * 40)       # offset = cycle * speed px
    assert band.tobytes() == band2.tobytes()
    # short text: centred, no scrolling
    short = {"type": "ticker", "text": "hi", "size": 28, "speed": 40}
    assert render.ticker_cycle(short) == 0.0
    band3, box3 = render.ticker_band(short, 999)
    band4, _ = render.ticker_band(short, 0)
    assert band3.tobytes() == band4.tobytes()           # offset is ignored
    # band position follows y, clamped to the tube
    _, box5 = render.ticker_band({"text": "hi", "size": 28, "y": 5}, 0)
    assert box5[1] == 0
    _, box6 = render.ticker_band({"text": "hi", "size": 28, "y": 239}, 0)
    assert box6[1] + box6[3] == TUBE_H


def test_ticker_draw_static():
    ctx = _ctx()
    img = render.draw_ticker({"type": "ticker", "text": "news " * 30, "size": 28,
                              "label": "News", "background": "#101010"}, ctx, (0, 0, 0))
    assert img.size == (TUBE_W, TUBE_H) and img.mode == "RGB"
    # the band's background colour fills the band (top row of the band, left edge)
    assert img.getpixel((2, 120 - 20)) == (0x10, 0x10, 0x10)
    assert img.getpixel((2, 120 + 20 - 1)) == (0x10, 0x10, 0x10)
    # empty text still draws the band, no raise
    img = render.draw_ticker({"type": "ticker", "text": ""}, ctx, (0, 0, 0))
    assert img.size == (TUBE_W, TUBE_H)


def test_new_widgets_in_a_full_screen_with_templates():
    async def renderer(t):
        return {"{{ temp }}": "21.5", "{{ headlines }}": "a • b • c " * 10,
                "{{ pct }}": "63", "{{ when_done }}": "2999-01-01T10:00:00+00:00"}.get(t, t)

    today = __import__("datetime").datetime.now().date().isoformat()
    ha = _FakeHa(weather={"state": "rainy", "attributes": {"temperature": 8, "temperature_unit": "°C"}},
                 forecast=[{"datetime": today + "T00:00:00+00:00", "temperature": 9, "templow": 4}])
    ctx = render.RenderContext(renderer, ha=ha)
    screen = Screen(name="s", tubes=[
        {"type": "graph", "entity": "sensor.temp", "hours": "{{ h }}", "label": "{{ lbl }}",
         "unit": "°", "color": "{{ col }}"},
        {"type": "progress", "value": "{{ pct }}", "max": 100, "finish": "{{ when_done }}",
         "label": "Print"},
        {"type": "analog_clock", "tz": "Europe/Berlin", "label": "Berlin"},
        {"type": "weather", "entity": "weather.home", "label": "{{ lbl }}"},
        {"type": "ticker", "text": "{{ headlines }}", "size": 28, "speed": 40},
        {"type": "blank"},
    ])
    # templates in the new widgets' fields are collected like any other field
    assert "{{ h }}" in render.collect_templates(screen)
    assert "{{ lbl }}" in render.collect_templates(screen)
    assert "{{ col }}" in render.collect_templates(screen)
    strip = asyncio.run(render.render_screen(screen, ctx))
    assert strip.size == (STRIP_W, STRIP_H) and strip.mode == "RGB"
    assert ctx.last_errors == {}
# resolved_tubes: the screen's six widgets with templates resolved
# ----------------------------------------------------------------------

def test_resolved_tubes_resolves_templates_like_render_screen():
    async def renderer(t):
        return {"{{ v }}" : "42"}.get(t, t)
    ctx = render.RenderContext(renderer)
    screen = Screen(name="s", tubes=[
        {"type": "text", "text": "{{ v }}"},
        {"type": "big_number", "value": "{{ v }}", "unit": "%"},
        {"type": "blank"},
        {"type": "custom", "items": [{"type": "text", "text": "{{ v }}", "x": 1, "y": 2}]},
        {"type": "ring", "value": "{{ v }}", "max": 100},
        {"type": "text", "text": "plain"},
    ])
    tubes = asyncio.run(render.resolved_tubes(screen, ctx))
    assert len(tubes) == 6
    assert tubes[0]["text"] == "42"
    assert tubes[1]["value"] == "42"
    assert tubes[3]["items"][0]["text"] == "42"     # nested inside custom items
    assert tubes[4]["value"] == "42"
    assert tubes[5]["text"] == "plain"
    # the original widget dicts are not mutated
    assert screen.tubes[0]["text"] == "{{ v }}"


def test_resolved_tubes_strip_screen_is_six_nones():
    ctx = _ctx()
    screen = Screen(name="s", strip={"source": "camera:x"})
    assert asyncio.run(render.resolved_tubes(screen, ctx)) == [None] * 6



def test_ticker_and_clock_animators_registered_and_sized():
    """The animation engine uploads exactly the (x, y, w, h) an animator returns."""
    import asyncio
    import animate
    from render import ticker_band
    w = {"type": "ticker", "text": "A long headline that certainly does not fit one tube", "size": 28, "y": 120}
    band, (x, y, bw, bh) = ticker_band(w, 0)
    assert (bw, bh) == band.size == (135, 40)
    assert y == 100
    ticker = animate.ANIMATORS["ticker"]
    assert ticker.interval(w) > 0
    assert ticker.interval({"type": "ticker", "text": "hi"}) == 0
    img, rect = asyncio.run(ticker.frame(w, None, 1.0, (10, 20, 30)))
    assert img.size == (rect[2], rect[3])
    assert img.getpixel((134, 0)) == (10, 20, 30)          # band takes the screen background
    clk = animate.ANIMATORS["analog_clock"]
    assert clk.interval({"type": "analog_clock"}) == 1.0
    assert clk.interval({"type": "analog_clock", "seconds": False}) == 30.0
    img, rect = asyncio.run(clk.frame({"type": "analog_clock"}, None, 0, None))
    assert img.size == (135, 240) and rect == (0, 0, 135, 240)


# ----------------------------------------------------------------------
# canvas screens (free design across all six tubes) and the font field
# ----------------------------------------------------------------------

def _canvas_screen(items, name="wide"):
    return Screen(name=name, canvas={"background": "#000000", "items": items})


def test_canvas_renders_items_at_strip_coordinates():
    ctx = _ctx()
    ctx.record_boxes = {}
    screen = _canvas_screen([
        {"type": "rect", "x": 607, "y": 200, "width": 380, "height": 12, "color": "#0000FF"},
        {"type": "rect", "x": 100, "y": 40, "width": 60, "height": 20, "color": "#FF0000"},
        {"type": "text", "x": 405, "y": 120, "text": "hi", "size": 30, "color": "#FFFFFF"},
    ])
    strip = asyncio.run(render.render_screen(screen, ctx))
    assert strip.size == (STRIP_W, STRIP_H)
    # the bar-like rect spans tubes 4 and 5 (x 417..796 of the 810-wide strip)
    assert strip.getpixel((610, 200)) == (0, 0, 255)
    assert strip.getpixel((790, 200)) == (0, 0, 255)
    assert strip.getpixel((800, 200)) == (0, 0, 0)          # past the rect's right edge
    assert strip.getpixel((100, 200)) == (0, 0, 0)          # outside the rect: background
    assert strip.getpixel((110, 45)) == (255, 0, 0)         # second rect in tube 0
    assert strip.getpixel((300, 45)) == (0, 0, 0)
    # text centred at x=405 (the strip centre) has ink around it
    assert any(strip.getpixel((x, y)) == (255, 255, 255)
               for x in range(380, 430) for y in range(100, 140))
    assert ctx.last_errors == {}
    # record_boxes under "<screen>:canvas"
    boxes = ctx.record_boxes["wide:canvas"]
    assert len(boxes) == 3
    assert boxes[0] == (417, 194, 797, 206)                 # rect centred at 607, width 380
    assert boxes[1] == (70, 30, 130, 50)                    # rect centred at 100, 60x20


def test_canvas_templates_in_items_are_resolved():
    async def renderer(t):
        return {"{{ temp }}": "21.5", "{{ pct }}": "60"}.get(t, t)

    screen = _canvas_screen([
        {"type": "text", "x": 202, "y": 120, "text": "{{ temp }}°", "size": 60},
        {"type": "bar", "x": 607, "y": 200, "width": 380, "height": 12, "value": "{{ pct }}", "max": 100},
    ])
    assert set(render.collect_templates(screen)) == {"{{ temp }}°", "{{ pct }}"}
    ctx = _ctx(renderer)
    strip = asyncio.run(render.render_screen(screen, ctx))
    assert ctx.last_errors == {}
    # bar filled to 60% of 380 px from x=417: 417+228=645
    assert strip.getpixel((500, 200)) == (255, 165, 0) or strip.getpixel((500, 200))[0] > 200
    assert strip.getpixel((700, 200)) == (60, 60, 60)       # track beyond the fill
    # the text rendered the template, not the raw "{{ temp }}"
    assert any(strip.getpixel((x, y)) != (0, 0, 0)
               for x in range(150, 260) for y in range(90, 150))


def test_canvas_failed_template_shows_bang():
    async def bad(t):
        raise RuntimeError("boom")

    screen = _canvas_screen([{"type": "text", "x": 405, "y": 120, "text": "{{ bad }}"}])
    ctx = _ctx(bad)
    strip = asyncio.run(render.render_screen(screen, ctx))
    assert strip.size == (STRIP_W, STRIP_H)
    assert "wide:canvas" in ctx.last_errors
    colors = strip.getcolors(maxcolors=100000)
    assert any(c > 0 and rgb[0] > 200 and rgb[1] < 120 for c, rgb in colors)


def test_canvas_bad_item_marks_only_that_item():
    ctx = _ctx()
    screen = _canvas_screen([
        {"type": "rect", "x": 100, "y": 40, "width": 60, "height": 20, "color": "#00FF00"},
        {"type": "nope", "x": 400, "y": 120},
    ])
    strip = asyncio.run(render.render_screen(screen, ctx))
    assert strip.getpixel((110, 45)) == (0, 255, 0)
    assert any("item 1" in k for k in ctx.last_errors)


def test_canvas_resolved_tubes_are_six_nones():
    ctx = _ctx()
    tubes = asyncio.run(render.resolved_tubes(_canvas_screen([]), ctx))
    assert tubes == [None] * 6
    strip_screen = Screen(name="s", strip={"source": "builtin:claude"})
    assert asyncio.run(render.resolved_tubes(strip_screen, ctx)) == [None] * 6


def test_canvas_background_field():
    ctx = _ctx()
    screen = Screen(name="c", canvas={"background": "#102030", "items": [
        {"type": "rect", "x": 405, "y": 120, "width": 20, "height": 20, "color": "#FFFFFF"}]})
    strip = asyncio.run(render.render_screen(screen, ctx))
    assert strip.getpixel((10, 10)) == (0x10, 0x20, 0x30)
    assert strip.getpixel((405, 120)) == (255, 255, 255)


def test_font_field_threads_through_widgets(monkeypatch):
    """A widget's `font` reaches load_font; the context default is used when the
    widget has none. Uses a spy on fonts.load_font (no real font file needed)."""
    calls = []
    real = fonts.load_font

    def spy(size, bold=False, family=None):
        calls.append(family)
        return real(size, bold=bold, family=family)

    monkeypatch.setattr(render.fonts, "load_font", spy)
    ctx = _ctx()
    ctx.default_font = "nunito"
    render.draw_text({"type": "text", "text": "hi", "font": "orbitron", "size": 20}, ctx, (0, 0, 0))
    assert "orbitron" in calls
    calls.clear()
    render.draw_text({"type": "text", "text": "hi", "size": 20}, ctx, (0, 0, 0))   # no font -> the default
    assert "nunito" in calls
    calls.clear()
    ctx.default_font = None
    render.draw_text({"type": "text", "text": "hi", "size": 20, "font": "default"}, ctx, (0, 0, 0))
    assert None in calls and "default" not in calls


def test_font_field_on_custom_text_item(monkeypatch):
    calls = []
    real = fonts.load_font

    def spy(size, bold=False, family=None):
        calls.append(family)
        return real(size, bold=bold, family=family)

    monkeypatch.setattr(render.fonts, "load_font", spy)
    ctx = _ctx()
    ctx.default_font = "oswald"
    w = {"type": "custom", "items": [
        {"type": "text", "x": 67, "y": 60, "text": "a", "font": "bebas-neue"},
        {"type": "text", "x": 67, "y": 160, "text": "b"},
    ]}
    asyncio.run(render.draw_custom(w, ctx, (0, 0, 0)))
    assert "bebas-neue" in calls and "oswald" in calls


def test_file_font_renders_differently(tmp_path):
    """With a real .ttf in the user dir, a file: font is loaded and used (skip
    when no .ttf exists in this environment)."""
    import os
    src = os.environ.get("TD_TEST_FONT") or next(
        (p for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                     "/usr/share/fonts/dejavu/DejaVuSans.ttf",
                     "/tmp/td/fonts/DejaVuSans.ttf") if os.path.isfile(p)), None)
    if src is None:
        pytest.skip("no .ttf available in this environment")
    import shutil
    udir = tmp_path / "fonts"
    udir.mkdir()
    shutil.copy(src, udir / "Test.ttf")
    fonts.set_user_dir(str(udir))
    try:
        ctx = _ctx()
        w = {"type": "text", "text": "Ag", "size": 40, "font": "file:Test.ttf"}
        img = _render_widget(w)
        assert img.size == (TUBE_W, TUBE_H)
        assert any(img.getpixel((x, y)) != (0, 0, 0) for x in range(TUBE_W) for y in range(TUBE_H))
    finally:
        fonts.set_user_dir(None)


# ======================================================================
# gif widget: the first frame, centred on the background
# ======================================================================

def _gif_bytes(tmp_path, name="anim.gif", size=(60, 40), frames=((255, 0, 0), (0, 255, 0))):
    from PIL import Image
    imgs = [Image.new("RGB", size, c) for c in frames]
    buf = io.BytesIO()
    imgs[0].save(buf, format="GIF", save_all=True, append_images=imgs[1:])
    (tmp_path / name).write_bytes(buf.getvalue())
    return buf.getvalue()


def test_gif_widget_draws_first_frame_centred(tmp_path):
    import asyncio
    import render
    _gif_bytes(tmp_path, frames=((255, 0, 0), (0, 255, 0)))
    ctx = render.RenderContext(lambda t: asyncio.sleep(0, result=t))
    ctx.images_dir = str(tmp_path)
    w = {"type": "gif", "source": "file:anim.gif"}
    img = asyncio.run(render.draw_gif(w, ctx, (0, 0, 0)))
    assert img.size == (render.TUBE_W, render.TUBE_H) and img.mode == "RGB"
    # first frame (red) centred: 60x40 scaled by 2.25 -> 135x90 at y 75..164
    assert img.getpixel((67, 120)) == (255, 0, 0)
    assert img.getpixel((0, 75)) == (255, 0, 0) and img.getpixel((134, 164)) == (255, 0, 0)
    # background above and below the frame
    assert img.getpixel((67, 74)) == (0, 0, 0)
    assert img.getpixel((67, 165)) == (0, 0, 0)
    assert img.getpixel((0, 0)) == (0, 0, 0) and img.getpixel((134, 239)) == (0, 0, 0)
    # the widget's own background wins over the screen's
    img2 = asyncio.run(render.draw_gif({**w, "background": "#0000FF"}, ctx, (0, 0, 0)))
    assert img2.getpixel((0, 0)) == (0, 0, 255)
    assert img2.getpixel((67, 120)) == (255, 0, 0)


def test_gif_widget_in_render_screen_and_resolved_tubes(tmp_path):
    import asyncio
    import render
    from screens import parse_screens
    _gif_bytes(tmp_path)
    ctx = render.RenderContext(lambda t: asyncio.sleep(0, result=t))
    ctx.images_dir = str(tmp_path)
    cfg = parse_screens({"rotation": ["a"], "screens": {"a": {
        "tubes": [{"type": "gif", "source": "file:anim.gif"}] + [{"type": "blank"}] * 5}}})
    assert cfg.errors == []
    strip = asyncio.run(render.render_screen(cfg.screens["a"], ctx))
    assert strip.size == (render.STRIP_W, render.STRIP_H)
    # tube 0 shows the red first frame centred, tube 1 is blank/black
    assert strip.getpixel((67, 120)) == (255, 0, 0)
    assert strip.getpixel((67 + 135, 120)) == (0, 0, 0)
    tubes = asyncio.run(render.resolved_tubes(cfg.screens["a"], ctx))
    assert tubes[0]["type"] == "gif" and tubes[1]["type"] == "blank"


def test_gif_widget_missing_file_shows_error_tile(tmp_path):
    import asyncio
    import render
    ctx = render.RenderContext(lambda t: asyncio.sleep(0, result=t))
    ctx.images_dir = str(tmp_path)
    img = asyncio.run(render.draw_gif({"type": "gif", "source": "file:nope.gif"}, ctx, (0, 0, 0)))
    assert img.size == (render.TUBE_W, render.TUBE_H)
    # the red "!" error tile, not a crash
    assert img.getpixel((render.TUBE_W // 2, render.TUBE_H // 2)) == (255, 80, 80)


def test_gif_widget_registered_and_template_fields_cover_it():
    import render
    from screens import WIDGET_TYPES
    assert "gif" in render._WIDGET_DRAW_ASYNC
    assert "gif" in render._WIDGET_TEMPLATE_FIELDS
    assert set(render._WIDGET_TEMPLATE_FIELDS) == set(WIDGET_TYPES)


def test_photo_tubes_helper():
    import render
    from screens import parse_screens
    # strip with a camera source: all six tubes are photo tubes
    cfg = parse_screens({"rotation": ["a"], "screens": {"a": {
        "strip": {"source": "camera:camera.door"}}}})
    assert render.photo_tubes(cfg.screens["a"], None) == set(range(6))
    # strip with a file source: none
    cfg = parse_screens({"rotation": ["a"], "screens": {"a": {
        "strip": {"source": "file:photo.png"}}}})
    assert render.photo_tubes(cfg.screens["a"], None) == set()
    # tubes: only the image tubes with camera:/entity_picture:/http sources
    cfg = parse_screens({"rotation": ["a"], "screens": {"a": {"tubes": [
        {"type": "image", "source": "camera:camera.x"},
        {"type": "image", "source": "file:pic.png"},
        {"type": "image", "source": "https://example.com/p.jpg"},
        {"type": "image", "source": "entity_picture:camera.y"},
        {"type": "text", "text": "hi"},
        {"type": "gif", "source": "file:a.gif"},
    ]}}})
    assert cfg.errors == []
    assert render.photo_tubes(cfg.screens["a"], cfg.screens["a"].tubes) == {0, 2, 3}
