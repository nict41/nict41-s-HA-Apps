"""Clock pusher: change detection, needed-mask re-send, 409 handling.

Uses a fake aiohttp session - no network, no real clock - plus, for the
compressed-upload and GIF tests, a fake clock server (aiohttp test server)
speaking old and new /panel.
"""

import asyncio
import time
import io

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from conftest import pil
from clock import ClockPusher, gif_clock_name
from rgb565 import TUBE_COUNT, TUBE_H, TUBE_W, image_to_rgb565, rgb565_to_image

class FakeResponse:
    def __init__(self, status, body=b"ok"):
        self.status = status
        self._body = body

    async def text(self):
        return self._body.decode()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    """Records POSTs; `statuses` is a list of statuses to return in order."""

    def __init__(self, statuses=(200,)):
        self.posts = []          # (params, data)
        self.gets = []
        self._statuses = list(statuses)

    def post(self, url, params=None, data=None, auth=None, headers=None, timeout=None):
        self.posts.append((dict(params or {}), data))
        status = self._statuses.pop(0) if self._statuses else 200
        if callable(status):
            status = status()
        return FakeResponse(status)

    def get(self, url, auth=None, timeout=None):
        self.gets.append(url)
        return FakeResponse(200, b'{"tubes":[0,1,2,3,4,5],"needed":[],"width":135,"height":240}')


async def _no_sleep(_):
    return None


def _pusher(session, publish_display=None):
    return ClockPusher(session, "http://clock.test", publish_display=publish_display,
                       sleep=_no_sleep)


def _payload(seed):
    return bytes([seed]) * (135 * 240 * 2)


def test_change_detection_only_changed_tubes_sent():
    session = FakeSession()
    p = _pusher(session)
    payloads = [_payload(i) for i in range(TUBE_COUNT)]
    sent = asyncio.run(p.send_tubes(payloads))
    assert sent == list(range(TUBE_COUNT))          # first send: all six
    assert len(session.posts) == TUBE_COUNT
    assert [params["tube"] for params, _ in session.posts] == list(range(TUBE_COUNT))

    # nothing changed -> nothing sent
    sent = asyncio.run(p.send_tubes(payloads))
    assert sent == []
    assert len(session.posts) == TUBE_COUNT

    # change tube 2 only
    payloads[2] = _payload(99)
    sent = asyncio.run(p.send_tubes(payloads))
    assert sent == [2]
    assert session.posts[-1][0]["tube"] == 2
    assert session.posts[-1][1] == payloads[2]


def test_needed_mask_resends_even_unchanged():
    session = FakeSession()
    p = _pusher(session)
    payloads = [_payload(i) for i in range(TUBE_COUNT)]
    asyncio.run(p.send_tubes(payloads))
    n = len(session.posts)
    # clock says tubes 1 and 4 lost their picture
    sent = asyncio.run(p.send_tubes(payloads, needed_mask=0b010010))
    assert sent == [1, 4]
    assert len(session.posts) == n + 2


def test_409_republishes_display_and_retries():
    published = []

    async def publish_display(mode):
        published.append(mode)

    # first POST for tube 0 -> 409, then 200
    session = FakeSession(statuses=[409, 200])
    p = _pusher(session, publish_display=publish_display)
    payloads = [_payload(0)] * TUBE_COUNT
    sent = asyncio.run(p.send_tubes(payloads))
    assert 0 in sent
    assert published == [4]          # display=4 re-published once for the 409
    # tube 0 got two POSTs (409 then 200), the rest one each
    tube0_posts = [1 for params, _ in session.posts if params["tube"] == 0]
    assert len(tube0_posts) == 2


def test_503_retries_with_backoff_then_succeeds():
    # tube 0 gets 503, 503, then 200; the other tubes then send fine
    session = FakeSession(statuses=[503, 503, 200])
    sleeps = []

    async def sleep(s):
        sleeps.append(s)

    p = ClockPusher(session, "http://clock.test", sleep=sleep)
    sent = asyncio.run(p.send_tubes([_payload(0)] * TUBE_COUNT))
    assert sent == list(range(TUBE_COUNT))
    assert len(sleeps) == 2
    assert sleeps[1] > sleeps[0]     # backoff grows
    assert sleeps[-1] <= 10.0        # capped ~10 s


def test_connection_error_does_not_crash():
    import aiohttp

    class FailingSession(FakeSession):
        def post(self, *a, **kw):
            raise aiohttp.ClientConnectionError("clock offline")

    p = _pusher(FailingSession())
    sent = asyncio.run(p.send_tubes([_payload(0)] * TUBE_COUNT))
    assert sent == []                # gave up, no crash
    assert p.last_error is not None


def test_reset_forces_resend():
    session = FakeSession()
    p = _pusher(session)
    payloads = [_payload(1)] * TUBE_COUNT
    asyncio.run(p.send_tubes(payloads))
    p.reset()
    sent = asyncio.run(p.send_tubes(payloads))
    assert sent == list(range(TUBE_COUNT))


def test_tubes_to_send_logic():
    p = _pusher(FakeSession())
    payloads = [_payload(i) for i in range(TUBE_COUNT)]
    assert p.tubes_to_send(payloads) == list(range(TUBE_COUNT))
    p._last_hash = [p._hash(d) for d in payloads]
    assert p.tubes_to_send(payloads) == []
    assert p.tubes_to_send(payloads, needed_mask=0b000101) == [0, 2]


# ----------------------------------------------------------------------
# reachability: abort after a connection error, reachable flag, probe
# ----------------------------------------------------------------------

def test_connection_error_aborts_remaining_tubes_and_marks_unreachable():
    import aiohttp

    class DyingSession(FakeSession):
        """Every POST raises a connection error (the clock is gone)."""
        def __init__(self):
            super().__init__()
            self.calls = 0

        def post(self, *a, **kw):
            self.calls += 1
            raise aiohttp.ClientConnectionError("clock offline")

    session = DyingSession()
    p = _pusher(session)
    payloads = [_payload(i) for i in range(TUBE_COUNT)]
    sent = asyncio.run(p.send_tubes(payloads))
    assert sent == []                       # tube 0 gave up, the rest were skipped
    assert p.reachable is False
    assert session.calls == 6               # 6 retries for tube 0 only, not 6x6
    assert p.last_error is not None


def test_http_error_status_does_not_mark_unreachable():
    session = FakeSession(statuses=[500] * TUBE_COUNT)
    p = _pusher(session)
    sent = asyncio.run(p.send_tubes([_payload(0)] * TUBE_COUNT))
    assert sent == []
    assert p.reachable is True              # an HTTP error is not "unreachable"
    assert p.last_error is not None


def test_success_marks_reachable_again():
    import aiohttp

    class FlakySession(FakeSession):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def post(self, *a, **kw):
            self.calls += 1
            if self.calls <= 6:
                raise asyncio.TimeoutError()
            return super().post(*a, **kw)

    p = _pusher(FlakySession())
    asyncio.run(p.send_tubes([_payload(0)] * TUBE_COUNT))
    assert p.reachable is False
    sent = asyncio.run(p.send_tubes([_payload(1)] * TUBE_COUNT))
    assert sent == list(range(TUBE_COUNT))
    assert p.reachable is True


def test_probe_ok_sets_reachable_and_clears_error():
    session = FakeSession()
    p = _pusher(session)
    p.reachable = False
    p.last_error = "old"
    ok = asyncio.run(p.probe())
    assert ok is True
    assert p.reachable is True
    assert p.last_error is None
    assert session.gets == ["http://clock.test/panel"]


def test_probe_failure_marks_unreachable():
    import aiohttp

    class DeadSession(FakeSession):
        def get(self, *a, **kw):
            raise aiohttp.ClientConnectionError("no route")

    p = _pusher(DeadSession())
    ok = asyncio.run(p.probe())
    assert ok is False
    assert p.reachable is False


def test_probe_non_200_marks_unreachable():
    class BadStatusSession(FakeSession):
        def get(self, *a, **kw):
            return FakeResponse(503)

    p = _pusher(BadStatusSession())
    assert asyncio.run(p.probe()) is False
    assert p.reachable is False


# ----------------------------------------------------------------------
# partial uploads: send_rect
# ----------------------------------------------------------------------

def _rect_payload(w, h, seed):
    return bytes([seed]) * (w * h * 2)


def test_send_rect_posts_params_and_body():
    session = FakeSession()
    p = _pusher(session)
    data = _rect_payload(135, 40, 7)
    ok = asyncio.run(p.send_rect(2, 0, 40, 135, 40, data))
    assert ok is True
    params, body = session.posts[-1]
    assert params == {"tube": 2, "x": 0, "y": 40, "w": 135, "h": 40}
    assert body == data
    assert len(body) == 135 * 40 * 2


def test_send_rect_validates_arguments():
    p = _pusher(FakeSession())
    with pytest.raises(ValueError):
        asyncio.run(p.send_rect(6, 0, 0, 10, 10, b"\x00" * 200))       # tube out of range
    with pytest.raises(ValueError):
        asyncio.run(p.send_rect(0, 130, 0, 10, 10, b"\x00" * 200))      # x+w > 135
    with pytest.raises(ValueError):
        asyncio.run(p.send_rect(0, 0, 230, 10, 20, b"\x00" * 400))      # y+h > 240
    with pytest.raises(ValueError):
        asyncio.run(p.send_rect(0, 0, 0, 10, 10, b"\x00" * 100))        # wrong body size


def test_send_rect_patches_last_bytes_and_hash():
    """After a full send, a rect upload patches those rows into _last_bytes, so a
    later full refresh only resends the tube if the full picture really differs."""
    session = FakeSession()
    p = _pusher(session)
    full = [_payload(i) for i in range(TUBE_COUNT)]
    asyncio.run(p.send_tubes(full))
    # rect on tube 1, rows 40..79, all pixels 0xAB
    rect = _rect_payload(135, 40, 0xAB)
    assert asyncio.run(p.send_rect(1, 0, 40, 135, 40, rect)) is True
    # the patched bytes equal a full tube with rows 40..79 replaced
    expected = bytearray(_payload(1))
    for row in range(40, 80):
        expected[row * 135 * 2:(row + 1) * 135 * 2] = b"\xab" * (135 * 2)
    assert p._last_bytes[1] == bytes(expected)
    assert p._last_hash[1] == p._hash(bytes(expected))
    # a full refresh with exactly that picture sends nothing for tube 1
    full[1] = bytes(expected)
    sent = asyncio.run(p.send_tubes(full))
    assert sent == []
    # a full refresh with a different picture resends tube 1
    full[1] = _payload(42)
    sent = asyncio.run(p.send_tubes(full))
    assert sent == [1]


def test_send_rect_small_region_patches_only_those_pixels():
    session = FakeSession()
    p = _pusher(session)
    full = [_payload(3)] * TUBE_COUNT
    asyncio.run(p.send_tubes(full))
    # 10x5 rect at (20, 100) on tube 4
    rect = _rect_payload(10, 5, 0x11)
    assert asyncio.run(p.send_rect(4, 20, 100, 10, 5, rect)) is True
    last = p._last_bytes[4]
    assert len(last) == 135 * 240 * 2
    # inside the rect: 0x11, outside: 0x03
    def px(x, y):
        off = (y * 135 + x) * 2
        return last[off]
    assert px(20, 100) == 0x11 and px(29, 104) == 0x11
    assert px(19, 100) == 0x03 and px(30, 100) == 0x03 and px(20, 99) == 0x03 and px(20, 105) == 0x03


def test_send_rect_unknown_last_bytes_sets_hash_none():
    """When the tube's last bytes are unknown (never sent / reset), a successful rect
    sets the hash to None so a later full refresh resends the whole tube."""
    session = FakeSession()
    p = _pusher(session)
    assert p._last_bytes[0] is None and p._last_hash[0] is None
    assert asyncio.run(p.send_rect(0, 0, 0, 135, 40, _rect_payload(135, 40, 1))) is True
    assert p._last_hash[0] is None
    # a full refresh now resends tube 0 even if the bytes would match the rect
    sent = asyncio.run(p.send_tubes([_payload(9)] * TUBE_COUNT))
    assert 0 in sent


def test_send_rect_409_and_503_handled_like_full_send():
    published = []

    async def publish_display(mode):
        published.append(mode)

    session = FakeSession(statuses=[409, 503, 200])
    p = _pusher(session, publish_display=publish_display)
    ok = asyncio.run(p.send_rect(3, 0, 0, 135, 40, _rect_payload(135, 40, 5)))
    assert ok is True
    assert published == [4]                      # 409 -> display=4 re-published
    assert len(session.posts) == 3               # 409, 503, 200
    assert all(params["tube"] == 3 for params, _ in session.posts)


def test_send_rect_connection_error_marks_unreachable_and_returns_false():
    import aiohttp

    class DeadSession(FakeSession):
        def post(self, *a, **kw):
            raise aiohttp.ClientConnectionError("clock offline")

    p = _pusher(DeadSession())
    ok = asyncio.run(p.send_rect(0, 0, 0, 10, 10, b"\x00" * 200))
    assert ok is False
    assert p.reachable is False
    assert p.last_error is not None
    # the last bytes/hash are untouched by the failed rect
    assert p._last_bytes[0] is None and p._last_hash[0] is None


# ======================================================================
# compressed uploads + GIF API against a fake clock server (aiohttp)
# ======================================================================

pytestmark_pil = pytest.mark.skipif(not pil, reason="Pillow not importable in this environment")

OLD_PANEL = {"tubes": [0, 1, 2, 3, 4, 5], "needed": [], "width": 135, "height": 240}
NEW_PANEL = {"tubes": [0, 1, 2, 3, 4, 5], "needed": [], "width": 135, "height": 240,
             "formats": ["rgb565", "qoi", "jpeg"], "gif": True, "gif_free": 900000,
             "playing": [], "firmware": "2.4.0"}


class FakeClock:
    """A fake HA Tubes clock: /panel (old or new firmware), /gif endpoints.
    Records every request; `panel_statuses` queues non-200 answers for POST /panel."""

    def __init__(self, new_firmware=True):
        self.new = new_firmware
        self.posts = []          # (path, params, content_type, body)
        self.gif_posts = []      # (path, params, body)
        self.gif_files = {}      # name -> bytes
        self.playing = set()
        self.panel_statuses = []  # statuses to answer POST /panel with, in order
        self.gif_play_status = 200
        self.app = web.Application()
        self.app.router.add_get("/panel", self.get_panel)
        self.app.router.add_post("/panel", self.post_panel)
        if new_firmware:
            # HA Tubes 2.4+ has the /gif endpoints; old firmware does not
            self.app.router.add_get("/gif", self.get_gif)
            self.app.router.add_post("/gif", self.post_gif)
            self.app.router.add_post("/gif/play", self.post_gif_play)
            self.app.router.add_post("/gif/stop", self.post_gif_stop)
            self.app.router.add_delete("/gif", self.delete_gif)

    async def get_panel(self, req):
        info = dict(NEW_PANEL if self.new else OLD_PANEL)
        info["playing"] = sorted(self.playing)
        return web.json_response(info)

    async def post_panel(self, req):
        body = await req.read()
        self.posts.append(("/panel", dict(req.query), req.headers.get("Content-Type"), body))
        if self.panel_statuses:
            status = self.panel_statuses.pop(0)
            if status == 200:
                # a /panel upload stops that tube's GIF
                tube = int(req.query.get("tube", -1))
                self.playing.discard(tube)
            return web.Response(status=status, text="fake clock answer")
        tube = int(req.query.get("tube", -1))
        self.playing.discard(tube)
        return web.Response(status=200, text="ok")

    async def get_gif(self, req):
        return web.json_response({"files": [{"name": n, "size": len(d)}
                                             for n, d in self.gif_files.items()],
                                  "free": 900000})

    async def post_gif(self, req):
        body = await req.read()
        self.gif_posts.append(("/gif", dict(req.query), body))
        name = req.query.get("name", "")
        if not name.endswith(".gif") or len(body) > 600 * 1024:
            return web.Response(status=400, text="bad gif")
        self.gif_files[name] = body
        return web.Response(status=200, text="ok")

    async def post_gif_play(self, req):
        self.gif_posts.append(("/gif/play", dict(req.query), b""))
        if self.gif_play_status != 200:
            return web.Response(status=self.gif_play_status, text="busy")
        self.playing.add(int(req.query["tube"]))
        return web.Response(status=200, text="ok")

    async def post_gif_stop(self, req):
        self.gif_posts.append(("/gif/stop", dict(req.query), b""))
        if "tube" in req.query:
            self.playing.discard(int(req.query["tube"]))
        else:
            self.playing.clear()
        return web.Response(status=200, text="ok")

    async def delete_gif(self, req):
        self.gif_posts.append(("DELETE /gif", dict(req.query), b""))
        self.gif_files.pop(req.query.get("name", ""), None)
        return web.Response(status=200, text="ok")


def _photo_payload():
    """A photo-like 135x240 RGB565 payload (gradient + shapes): JPEG beats QOI on it."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (TUBE_W, TUBE_H))
    px = img.load()
    for y in range(TUBE_H):
        for x in range(TUBE_W):
            px[x, y] = (x * 255 // (TUBE_W - 1), y * 255 // (TUBE_H - 1), (x * 3 + y) // 2 % 256)
    d = ImageDraw.Draw(img)
    d.ellipse([20, 30, 110, 200], fill=(200, 30, 30))
    d.rectangle([10, 10, 60, 60], fill=(30, 200, 90))
    return image_to_rgb565(img)


def _flat_payload(seed):
    return bytes([seed]) * (TUBE_W * TUBE_H * 2)


async def _with_fake_clock(clock, fn):
    """Run fn(pusher, clock) with a real aiohttp session against the fake clock."""
    import aiohttp
    async with TestClient(TestServer(clock.app)) as client:
        async with aiohttp.ClientSession() as session:
            pusher = ClockPusher(session, str(client.make_url("")), sleep=_no_sleep)
            await fn(pusher, clock)


def _run(coro):
    return asyncio.run(coro)


@pytestmark_pil
def test_qoi_used_when_advertised_and_raw_otherwise():
    async def check(pusher, clock):
        payloads = [_flat_payload(i) for i in range(TUBE_COUNT)]
        sent = await pusher.send_tubes(payloads)
        assert sent == list(range(TUBE_COUNT))
        assert pusher.upload_format == "qoi"
        assert pusher.formats == {"rgb565", "qoi", "jpeg"} and pusher.gif_supported is True
        # every POST is QOI with the octet-stream content type
        for path, params, ctype, body in clock.posts:
            assert path == "/panel" and params.get("format") == "qoi"
            assert ctype == "application/octet-stream"
            assert body[:4] == b"qoif"
            assert int(params["tube"]) in range(TUBE_COUNT)
        # the QOI decodes to exactly the RGB565 payload's image
        from PIL import Image
        for i, (path, params, ctype, body) in enumerate(clock.posts):
            img = Image.open(io.BytesIO(body)).convert("RGB")
            assert image_to_rgb565(img) == payloads[i]
        # hashes are on the RGB565 bytes: an unchanged tube is not re-sent
        n = len(clock.posts)
        sent = await pusher.send_tubes(payloads)
        assert sent == [] and len(clock.posts) == n
    _run(_with_fake_clock(FakeClock(new_firmware=True), check))

    async def check_old(pusher, clock):
        payloads = [_flat_payload(i) for i in range(TUBE_COUNT)]
        sent = await pusher.send_tubes(payloads)
        assert sent == list(range(TUBE_COUNT))
        assert pusher.upload_format == "rgb565"
        assert pusher.formats == set() and pusher.gif_supported is False
        for path, params, ctype, body in clock.posts:
            assert "format" not in params and body == payloads[int(params["tube"])]
            assert ctype == "application/octet-stream"
    _run(_with_fake_clock(FakeClock(new_firmware=False), check_old))


@pytestmark_pil
def test_qoi_400_falls_back_to_raw_until_reset():
    async def check(pusher, clock):
        clock.panel_statuses = [400] + [200] * 20   # first QOI upload -> 400
        payloads = [_flat_payload(i) for i in range(TUBE_COUNT)]
        sent = await pusher.send_tubes(payloads)
        assert sent == list(range(TUBE_COUNT))
        assert pusher.upload_format == "rgb565"       # marked unsupported
        formats = [p.get("format") for _, p, _, _ in clock.posts]
        assert formats[0] == "qoi" and all(f is None for f in formats[1:])
        bodies = [b for _, _, _, b in clock.posts]
        assert bodies[1] == payloads[0]               # raw body after the 400
        # still raw on the next send, until reset()
        await pusher.send_tubes([_flat_payload(9)] * TUBE_COUNT)
        assert all(p.get("format") is None for _, p, _, _ in clock.posts[1:])
        pusher.reset()
        await pusher.send_tubes([_flat_payload(9)] * TUBE_COUNT)   # re-learns the caps
        assert pusher.upload_format == "qoi"         # QOI works again after reset
        assert clock.posts[-1][1].get("format") == "qoi"
    _run(_with_fake_clock(FakeClock(new_firmware=True), check))


@pytestmark_pil
def test_jpeg_chosen_for_photo_tube_and_retried_as_qoi_on_503():
    async def check(pusher, clock):
        photo = _photo_payload()
        text = _flat_payload(7)
        payloads = [text] * TUBE_COUNT
        payloads[2] = photo
        # tube 2's first (JPEG) attempt gets 503 "not enough memory", then QOI works.
        # Tubes are sent in order, so the 503 must be queued for tube 2's upload:
        # tube 0 and 1 answer 200 first.
        clock.panel_statuses = [200, 200, 503, 200]
        sent = await pusher.send_tubes(payloads, photo_tubes={2})
        assert sent == list(range(TUBE_COUNT))
        tube2 = [(p, b) for _, p, _, b in clock.posts if p.get("tube") == "2"]
        assert len(tube2) == 2
        assert tube2[0][0].get("format") == "jpeg" and tube2[0][1][:3] == b"\xff\xd8\xff"
        assert tube2[1][0].get("format") == "qoi" and tube2[1][1][:4] == b"qoif"
        # text-only tubes are always QOI, never JPEG
        others = [(p, b) for _, p, _, b in clock.posts if p.get("tube") != "2"]
        assert all(p.get("format") == "qoi" for p, _ in others)
    _run(_with_fake_clock(FakeClock(new_firmware=True), check))

    async def check_no_503(pusher, clock):
        # without a 503 the photo tube keeps its JPEG (smaller than the QOI here)
        photo = _photo_payload()
        payloads = [photo] * TUBE_COUNT
        await pusher.send_tubes(payloads, photo_tubes=set(range(TUBE_COUNT)))
        assert all(p.get("format") == "jpeg" for _, p, _, _ in clock.posts)
    _run(_with_fake_clock(FakeClock(new_firmware=True), check_no_503))

    async def check_old_firmware(pusher, clock):
        # old firmware: photo tubes stay raw too
        photo = _photo_payload()
        await pusher.send_tubes([photo] * TUBE_COUNT, photo_tubes=set(range(TUBE_COUNT)))
        assert all("format" not in p for _, p, _, _ in clock.posts)
    _run(_with_fake_clock(FakeClock(new_firmware=False), check_old_firmware))


@pytestmark_pil
def test_send_rect_uses_qoi_and_patches_hash():
    async def check(pusher, clock):
        full = [_flat_payload(i) for i in range(TUBE_COUNT)]
        await pusher.send_tubes(full)
        n = len(clock.posts)
        rect = _flat_payload(0xAB)[:135 * 40 * 2]
        ok = await pusher.send_rect(1, 0, 40, 135, 40, rect)
        assert ok is True
        path, params, ctype, body = clock.posts[-1]
        assert params["format"] == "qoi" and params["x"] == "0" and params["y"] == "40"
        assert ctype == "application/octet-stream" and body[:4] == b"qoif"
        # the rect was patched into the tube's last bytes (hash on RGB565, unchanged behaviour)
        expected = bytearray(_flat_payload(1))
        for row in range(40, 80):
            expected[row * 135 * 2:(row + 1) * 135 * 2] = b"\xab" * (135 * 2)
        assert pusher._last_bytes[1] == bytes(expected)
        assert pusher._last_hash[1] == pusher._hash(bytes(expected))
        full[1] = bytes(expected)
        sent = await pusher.send_tubes(full)
        assert sent == [] and len(clock.posts) == n + 1
    _run(_with_fake_clock(FakeClock(new_firmware=True), check))


@pytestmark_pil
def test_status_upload_format_and_bytes_per_minute():
    async def check(pusher, clock):
        assert pusher.upload_format == "rgb565"      # nothing learned yet
        assert pusher.bytes_per_minute() == 0
        payloads = [_flat_payload(i) for i in range(TUBE_COUNT)]
        await pusher.send_tubes(payloads)
        assert pusher.upload_format == "qoi"
        sent_bytes = sum(len(b) for _, _, _, b in clock.posts)
        assert pusher.bytes_per_minute() == sent_bytes
        # the rolling window drops entries older than 60 s
        # (relative to now: time.monotonic() counts from boot, which on a fresh CI runner
        # can be less than a minute ago)
        pusher._sent_log = [(time.monotonic() - 120.0, 12345)] + pusher._sent_log
        assert pusher.bytes_per_minute() == sent_bytes   # the old entry is dropped
    _run(_with_fake_clock(FakeClock(new_firmware=True), check))


@pytestmark_pil
def test_gif_upload_play_stop_delete_and_playing_skip():
    async def check(pusher, clock):
        assert pusher.gif_supported is False
        await pusher.fetch_panel_info()
        assert pusher.gif_supported is True
        data = b"GIF89a" + b"\x00" * 100
        name = gif_clock_name("party.gif")
        assert name == "td-party.gif"
        assert await pusher.gif_upload(name, data) is True
        assert clock.gif_files[name] == data
        assert ("/gif", {"name": name}, data) in clock.gif_posts
        # play it on tube 3
        assert await pusher.gif_play(3, name, loop=True, bg="000000") is True
        assert clock.playing == {3}
        assert pusher._playing == {3}
        play = [g for g in clock.gif_posts if g[0] == "/gif/play"][0]
        assert play[1]["tube"] == "3" and play[1]["name"] == name
        assert play[1]["loop"] == "1" and play[1]["bg"] == "000000"
        # a refresh does not re-send the static frame over the playing GIF
        payloads = [_flat_payload(i) for i in range(TUBE_COUNT)]
        sent = await pusher.send_tubes(payloads)
        assert 3 not in sent
        assert all(p.get("tube") != "3" for _, p, _, _ in clock.posts)
        # ... unless the clock needs the tube (needed mask wins)
        sent = await pusher.send_tubes(payloads, needed_mask=1 << 3)
        assert 3 in sent
        assert clock.playing == set()                 # the upload stopped the GIF
        assert pusher._playing == set()
        # stop + delete
        assert await pusher.gif_stop(3) is True
        assert await pusher.gif_stop() is True
        assert await pusher.gif_delete(name) is True
        assert name not in clock.gif_files
        deletes = [g for g in clock.gif_posts if g[0] == "DELETE /gif"]
        assert deletes and deletes[0][1]["name"] == name
        # every gif POST went out as octet-stream
    _run(_with_fake_clock(FakeClock(new_firmware=True), check))

    async def check_old(pusher, clock):
        # old firmware: no gif support advertised; the fake clock has no /gif
        # route either, so the upload fails (404) instead of pretending
        await pusher.fetch_panel_info()
        assert pusher.gif_supported is False
        assert await pusher.gif_upload("td-x.gif", b"GIF89a") is False
        assert pusher.last_error is not None
    _run(_with_fake_clock(FakeClock(new_firmware=False), check_old))


def test_gif_clock_name_rules():
    assert gif_clock_name("party.gif") == "td-party.gif"
    assert gif_clock_name("My Party!.gif") == "td-My_Party_.gif"
    assert len(gif_clock_name("x" * 80 + ".gif")) == 40
    assert gif_clock_name("a.gif").endswith(".gif") and gif_clock_name("a.gif").startswith("td-")


@pytestmark_pil
def test_rgb565_to_image_round_trips_exactly():
    import random
    from PIL import Image
    random.seed(7)
    img = Image.new("RGB", (TUBE_W, TUBE_H))
    px = img.load()
    for y in range(TUBE_H):
        for x in range(TUBE_W):
            px[x, y] = (random.randrange(256), random.randrange(256), random.randrange(256))
    data = image_to_rgb565(img)
    back = rgb565_to_image(data, TUBE_W, TUBE_H)
    assert back.size == (TUBE_W, TUBE_H) and back.mode == "RGB"
    assert image_to_rgb565(back) == data          # exact round-trip
    # and the QOI of that image decodes to the same pixels (what the clock shows)
    buf = io.BytesIO()
    back.save(buf, format="QOI")
    decoded = Image.open(io.BytesIO(buf.getvalue())).convert("RGB")
    assert image_to_rgb565(decoded) == data
    with pytest.raises(ValueError):
        rgb565_to_image(b"\x00" * 10, TUBE_W, TUBE_H)
