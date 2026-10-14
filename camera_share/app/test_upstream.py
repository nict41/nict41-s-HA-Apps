"""The one-subscription rule, and the failure modes that protect the printer.

Every upstream here is an httpx.MockTransport. Nothing in this file can reach
a real Home Assistant instance or a real camera, which matters more than
usual: the camera this app exists for allows exactly one connection and its
firmware crashes when something opens a second.
"""

import asyncio
import time

import httpx
import pytest

import settings
import upstream as upstream_module
from conftest import JPEG, mjpeg_body

pytestmark = pytest.mark.usefixtures("_fast_timers")


class _Body(httpx.AsyncByteStream):
    """A streaming response body that can go quiet instead of ending.

    `silence` models the failure that actually happens: the printer's camera
    server dies, the HTTP response stays open, and no further bytes arrive.
    """

    def __init__(self, chunks, silence: float = 0.0):
        self._chunks = list(chunks)
        self._silence = silence

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk
        if self._silence:
            await asyncio.sleep(self._silence)

    async def aclose(self):
        return None


def _transport(responder):
    """A MockTransport that records every request it is asked to make."""
    calls: list[tuple[float, httpx.Request]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((time.monotonic(), request))
        return responder(len(calls), request)

    return httpx.MockTransport(handler), calls


def _frames_then(frames, silence=5.0, status=200,
                 boundary="--frameboundary"):
    def responder(_attempt, _request):
        return httpx.Response(
            status,
            headers={"content-type": f"multipart/x-mixed-replace; boundary={boundary}"},
            stream=_Body([mjpeg_body(frames, boundary=boundary)], silence=silence),
        )
    return responder


@pytest.fixture
async def make_upstream():
    """Builds Upstreams on a mock transport and tears them all down."""
    created: list[upstream_module.Upstream] = []

    def build(responder):
        transport, calls = _transport(responder)
        up = upstream_module.Upstream(httpx.AsyncClient(transport=transport))
        created.append(up)
        return up, calls

    yield build
    for up in created:
        await up.aclose()


async def _until(predicate, timeout=3.0, interval=0.01):
    """Wait for a condition, failing the test rather than hanging forever."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("condition never became true")


# ── One subscription, whatever the audience ─────────────────────────────────

async def test_many_viewers_make_exactly_one_subscription(make_upstream):
    """The whole point of the app: N viewers, one connection to the camera."""
    up, calls = make_upstream(_frames_then([JPEG] * 3))

    for _ in range(6):
        up.acquire()
    await _until(lambda: up.seq >= 3)

    assert up.viewers == 6
    assert up.connections == 1
    assert len(calls) == 1
    assert up.state == upstream_module.LIVE


async def test_viewers_arriving_later_reuse_the_same_subscription(make_upstream):
    up, calls = make_upstream(_frames_then([JPEG] * 2))

    up.acquire()
    await _until(lambda: up.seq >= 1)
    for _ in range(3):
        up.acquire()
    await asyncio.sleep(0.1)

    assert up.connections == 1
    assert len(calls) == 1


async def test_the_request_asks_home_assistant_for_the_configured_camera(make_upstream):
    up, calls = make_upstream(_frames_then([JPEG]))

    up.acquire()
    await _until(lambda: up.seq >= 1)

    request = calls[0][1]
    assert request.url.path.endswith("/camera_proxy_stream/camera.centauri_carbon_2_chamber_camera")
    # The per-viewer cap is also what is asked of the hub upstream.
    assert request.url.params["fps"] == str(settings.fps())
    assert request.headers["authorization"] == "Bearer test-token"


async def test_frames_are_not_queued_only_the_latest_is_kept(make_upstream):
    """A slow viewer falls behind in time, never in memory."""
    up, _ = make_upstream(_frames_then([JPEG, JPEG, JPEG]))

    up.acquire()
    await _until(lambda: up.seq >= 3)

    assert up.seq == 3
    assert up.frame == JPEG  # one frame held, not three


# ── Letting go of the camera ────────────────────────────────────────────────

async def test_last_viewer_leaving_closes_the_subscription_after_the_idle_delay(make_upstream):
    up, calls = make_upstream(_frames_then([JPEG]))

    up.acquire()
    await _until(lambda: up.seq >= 1)
    up.release()

    # Still held, so a page reload doesn't cost a reconnect.
    assert up.running is True
    await _until(lambda: not up.running, timeout=2.0)
    # Not running and still "live" is a state nothing should ever observe.
    assert up.state == upstream_module.IDLE
    assert up.status()["upstream"] == upstream_module.IDLE
    assert up.viewers == 0
    assert len(calls) == 1


async def test_a_reload_inside_the_idle_window_keeps_the_subscription(make_upstream):
    up, calls = make_upstream(_frames_then([JPEG] * 2))

    up.acquire()
    await _until(lambda: up.seq >= 1)
    up.release()
    await asyncio.sleep(settings.IDLE_SHUTDOWN_SECONDS / 2)
    up.acquire()  # the reload lands
    await asyncio.sleep(settings.IDLE_SHUTDOWN_SECONDS * 2)

    assert up.running is True
    assert up.connections == 1
    assert len(calls) == 1


# ── Stalls and backoff ──────────────────────────────────────────────────────

async def test_a_stalled_stream_goes_offline_and_is_retried(make_upstream):
    """The printer going quiet is the normal failure: the response stays open
    and simply stops producing frames."""
    up, calls = make_upstream(_frames_then([JPEG], silence=30.0))

    up.acquire()
    await _until(lambda: up.seq >= 1)
    assert up.state == upstream_module.LIVE

    # STALL_SECONDS of silence closes it rather than holding it open forever.
    await _until(lambda: up.state == upstream_module.OFFLINE, timeout=3.0)
    assert "camera server" in (up._error or "")

    # And it does come back, on a new subscription.
    await _until(lambda: up.connections >= 2, timeout=3.0)


async def test_retries_are_never_a_tight_loop(make_upstream):
    """A tight retry loop against a one-connection camera is what crashed its
    firmware before. Every attempt must have waited at least the backoff."""
    def responder(_attempt, _request):
        return httpx.Response(502, text="nope")

    up, calls = make_upstream(responder)
    up.acquire()
    await _until(lambda: len(calls) >= 4, timeout=5.0)
    up.release()
    await up.stop()

    gaps = [later[0] - earlier[0] for earlier, later in zip(calls, calls[1:])]
    assert min(gaps) >= settings.BACKOFF_START_SECONDS
    # Doubling, so the last wait is comfortably longer than the first.
    assert gaps[-1] > gaps[0] * 1.5
    # Capped, so it never walks off to hours.
    assert max(gaps) <= settings.BACKOFF_MAX_SECONDS * (1 + settings.BACKOFF_JITTER) + 0.2
    assert up.state == upstream_module.IDLE


async def test_a_bad_status_is_reported_not_swallowed(make_upstream):
    up, _ = make_upstream(lambda _a, _r: httpx.Response(404, text="unknown entity"))

    up.acquire()
    await _until(lambda: up.state == upstream_module.OFFLINE)

    assert "404" in (up._error or "")
    assert "2.12.3.0" in (up._error or "")


async def test_a_stream_without_a_boundary_is_rejected(make_upstream):
    def responder(_attempt, _request):
        return httpx.Response(200, headers={"content-type": "image/jpeg"},
                              stream=_Body([JPEG]))

    up, _ = make_upstream(responder)
    up.acquire()
    await _until(lambda: up.state == upstream_module.OFFLINE)

    assert "boundary" in (up._error or "")


async def test_no_supervisor_token_means_no_connection_attempt(make_upstream, monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "")
    up, calls = make_upstream(_frames_then([JPEG]))

    up.acquire()
    await _until(lambda: up.state == upstream_module.OFFLINE)

    assert calls == []
    assert "SUPERVISOR_TOKEN" in (up._error or "")


async def test_backoff_resets_once_frames_flow_again(make_upstream):
    """A camera that drops a connection every few seconds shouldn't end up on
    a 60-second retry: a subscription that produced frames resets the clock."""
    up, calls = make_upstream(_frames_then([JPEG], silence=0.0))

    up.acquire()
    await _until(lambda: up.connections >= 4, timeout=5.0)
    up.release()
    await up.stop()

    gaps = [later[0] - earlier[0] for earlier, later in zip(calls, calls[1:])]
    ceiling = settings.BACKOFF_START_SECONDS * (1 + settings.BACKOFF_JITTER) + 0.15
    assert max(gaps) <= ceiling, gaps


# ── Snapshot freshness ──────────────────────────────────────────────────────

async def test_fresh_frame_expires(make_upstream, monkeypatch):
    up, _ = make_upstream(_frames_then([JPEG], silence=30.0))

    up.acquire()
    await _until(lambda: up.seq >= 1)
    assert up.fresh_frame() == JPEG

    monkeypatch.setattr(settings, "SNAPSHOT_MAX_AGE_SECONDS", 0.0)
    assert up.fresh_frame() is None


async def test_status_reports_without_starting_anything(make_upstream):
    up, calls = make_upstream(_frames_then([JPEG]))

    status = up.status()

    assert calls == []
    assert up.running is False
    assert status["upstream"] == upstream_module.IDLE
    assert status["viewers"] == 0
    assert status["last_frame_age"] is None
    assert status["camera_entity"] == "camera.centauri_carbon_2_chamber_camera"


# ── Saying what actually went wrong ─────────────────────────────────────────

def test_describe_names_the_exception_class():
    """The failure that prompted this: httpx's timeouts carry an empty
    message, so `str(exc)` alone logged "failed: " and hid a ReadTimeout."""
    assert upstream_module.describe(httpx.ReadTimeout("")) == "ReadTimeout"
    assert upstream_module.describe(httpx.ConnectTimeout("")) == "ConnectTimeout"
    assert upstream_module.describe(ValueError("boom")) == "ValueError: boom"
    # Whitespace-only is as useless as empty.
    assert upstream_module.describe(httpx.ReadTimeout("   ")) == "ReadTimeout"


async def test_a_timeout_waiting_for_headers_says_the_hub_is_stuck(make_upstream):
    """The hub getting stuck looks like this from here: the connection is
    accepted and then nothing at all arrives, not even headers."""
    def responder(_attempt, request):
        raise httpx.ReadTimeout("", request=request)

    up, _ = make_upstream(responder)
    up.acquire()
    await _until(lambda: up.state == upstream_module.OFFLINE)

    error = up._error or ""
    assert "no headers" in error
    assert "frame hub" in error
    assert "stuck" in error
    # And the bare class name never stands in for the explanation again.
    assert error != "ReadTimeout"


async def test_a_timeout_before_the_connection_says_so(make_upstream):
    def responder(_attempt, request):
        raise httpx.ConnectTimeout("", request=request)

    up, _ = make_upstream(responder)
    up.acquire()
    await _until(lambda: up.state == upstream_module.OFFLINE)

    assert "did not accept a connection" in (up._error or "")


async def test_a_silent_stream_is_not_reported_as_a_stuck_hub(make_upstream):
    """Headers arrived, so whatever went quiet afterwards is the camera, not
    the hub refusing to answer at all."""
    up, _ = make_upstream(_frames_then([JPEG], silence=30.0))
    up.acquire()
    await _until(lambda: up.state == upstream_module.OFFLINE, timeout=3.0)

    error = up._error or ""
    assert "frame hub" not in error
    assert "camera server" in error


async def test_an_empty_message_exception_still_logs_its_class(make_upstream, caplog):
    def responder(_attempt, request):
        raise httpx.ReadTimeout("", request=request)

    up, _ = make_upstream(responder)
    with caplog.at_level("WARNING"):
        up.acquire()
        await _until(lambda: up.state == upstream_module.OFFLINE)

    failures = [r.getMessage() for r in caplog.records
                if "Camera subscription failed" in r.getMessage()]
    assert failures, "the failure was never logged"
    # Never "Camera subscription failed: " with nothing after it.
    assert not any(m.rstrip().endswith("failed:") for m in failures)


async def test_shutdown_cannot_hang(make_upstream, monkeypatch):
    """Closing always means cancelling a request that is in flight, and
    httpx's pool can refuse to drain after that. A stop or restart that
    hangs until the Supervisor kills the container is worse than a leaked
    socket the exiting process takes with it anyway."""
    up, _ = make_upstream(_frames_then([JPEG], silence=30.0))
    up.acquire()
    await _until(lambda: up.seq >= 1)
    await up.stop()  # retire the real reader before wedging the shutdown

    monkeypatch.setattr(settings, "CLOSE_SECONDS", 0.05)

    # A reader that will not die on cancellation, and a client whose close
    # never returns: both halves of the shutdown wedged at once.
    release = asyncio.Event()

    async def _stubborn():
        while not release.is_set():
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                pass

    async def _never_closes():
        await asyncio.Event().wait()

    stubborn = asyncio.create_task(_stubborn())
    up._reader = stubborn
    monkeypatch.setattr(up._client, "aclose", _never_closes)
    await asyncio.sleep(0)  # let the stubborn reader actually start

    # The assertion is simply that this returns at all.
    await asyncio.wait_for(up.aclose(), timeout=3.0)
    assert up.state == upstream_module.IDLE

    release.set()
    await asyncio.gather(stubborn, return_exceptions=True)
