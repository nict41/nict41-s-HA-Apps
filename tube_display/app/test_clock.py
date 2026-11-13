"""Clock pusher: change detection, needed-mask re-send, 409 handling.

Uses a fake aiohttp session - no network, no real clock.
"""

import asyncio

import pytest

from clock import ClockPusher
from rgb565 import TUBE_COUNT

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
