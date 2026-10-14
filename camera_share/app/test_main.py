"""The four routes: who may see them, what they serve, and the viewer cap.

Nothing here reaches Home Assistant. The routes that need frames are given
them directly, and the ones that would open a subscription get an upstream
whose reader task is replaced with a sleep - so a test can never be the
second connection to a camera that only tolerates one.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

import main
import settings
import upstream as upstream_module
from conftest import JPEG

pytestmark = pytest.mark.usefixtures("_fast_timers")


@pytest.fixture
def hub(monkeypatch):
    """A fresh upstream for every test, so no frame or viewer count leaks.

    Used by the routes that never open a subscription, which is why it needs
    no event loop and creates no tasks.
    """
    fresh = upstream_module.Upstream()
    monkeypatch.setattr(main, "upstream", fresh)
    return fresh


@pytest.fixture
async def watched_hub(monkeypatch):
    """An upstream whose reader task is a sleep.

    Taking a viewer slot has to be observable without anything dialling out:
    the camera this app talks to allows exactly one connection and crashes
    when something opens a second, so no test may be able to reach it.
    """
    async def _idle_run(self):
        await asyncio.sleep(3600)

    monkeypatch.setattr(upstream_module.Upstream, "_run", _idle_run)
    fresh = upstream_module.Upstream()
    monkeypatch.setattr(main, "upstream", fresh)
    yield fresh
    await fresh.aclose()


@pytest.fixture
def client(hub):
    with TestClient(main.app) as test_client:
        yield test_client


def _request(path="/", query=b"", cookies=None, scheme="http", headers=None):
    """A minimal ASGI request, for calling a route function directly.

    The streaming routes can't go through TestClient: it buffers a response
    body to completion, and an MJPEG stream never completes.
    """
    raw = [(b"host", b"cam.test")]
    if cookies:
        raw.append((b"cookie", "; ".join(f"{k}={v}" for k, v in cookies.items()).encode()))
    for name, value in (headers or {}).items():
        raw.append((name.encode(), value.encode()))
    return Request({
        "type": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query,
        "root_path": "",
        "scheme": scheme,
        "headers": raw,
        "client": ("203.0.113.9", 54321),
        "server": ("cam.test", 80),
        "app": main.app,
    })


# ── Authorisation ───────────────────────────────────────────────────────────

def test_open_when_no_access_key_is_set(client, monkeypatch):
    monkeypatch.setenv("ACCESS_KEY", "")
    response = client.get("/")
    assert response.status_code == 200
    assert "Test Cam" in response.text
    assert settings.COOKIE_NAME not in response.cookies


def test_missing_key_is_401(client, monkeypatch):
    monkeypatch.setenv("ACCESS_KEY", "s3cret")
    response = client.get("/")
    assert response.status_code == 401
    assert "access key" in response.text
    # Never a hint of what the key is.
    assert "s3cret" not in response.text


def test_wrong_key_is_403(client, monkeypatch):
    monkeypatch.setenv("ACCESS_KEY", "s3cret")
    response = client.get("/", params={"key": "nope"})
    assert response.status_code == 403
    assert settings.COOKIE_NAME not in response.cookies


def test_correct_key_sets_the_cookie(client, monkeypatch):
    monkeypatch.setenv("ACCESS_KEY", "s3cret")
    response = client.get("/", params={"key": "s3cret"})

    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{settings.COOKIE_NAME}=")
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie.replace("samesite", "SameSite")
    assert "Path=/" in cookie
    # The key itself is never put in a cookie, only a digest of it.
    assert "s3cret" not in cookie
    assert settings.key_digest("s3cret") in cookie


def test_cookie_alone_is_enough_on_a_later_request(client, monkeypatch):
    monkeypatch.setenv("ACCESS_KEY", "s3cret")
    client.get("/", params={"key": "s3cret"})
    response = client.get("/")
    assert response.status_code == 200


def test_a_forged_cookie_is_rejected(client, monkeypatch):
    monkeypatch.setenv("ACCESS_KEY", "s3cret")
    client.cookies.set(settings.COOKIE_NAME, settings.key_digest("guess"))
    response = client.get("/")
    assert response.status_code == 403


def test_cookie_is_secure_behind_the_tunnel(client, monkeypatch):
    """Cloudflare terminates TLS and forwards X-Forwarded-Proto: https, which
    is the deployment this page exists for."""
    monkeypatch.setenv("ACCESS_KEY", "s3cret")
    response = client.get("/", params={"key": "s3cret"},
                          headers={"X-Forwarded-Proto": "https"})
    assert "Secure" in response.headers["set-cookie"]


def test_cookie_drops_secure_on_plain_http(client, monkeypatch):
    """A browser refuses a Secure cookie from a non-secure origin, which would
    make the app unusable on the LAN - where there is no transport
    confidentiality to protect in the first place."""
    monkeypatch.setenv("ACCESS_KEY", "s3cret")
    response = client.get("/", params={"key": "s3cret"})
    assert "Secure" not in response.headers["set-cookie"]


def test_the_page_carries_the_key_into_its_own_stream_url(client, monkeypatch):
    """So the page still works for a viewer who blocks cookies outright."""
    monkeypatch.setenv("ACCESS_KEY", "a key/with?specials")
    response = client.get("/", params={"key": "a key/with?specials"})
    assert "stream.mjpg?key=a%20key%2Fwith%3Fspecials" in response.text


def test_snapshot_needs_the_key(client, monkeypatch):
    monkeypatch.setenv("ACCESS_KEY", "s3cret")
    assert client.get("/snapshot.jpg").status_code == 401
    assert client.get("/snapshot.jpg", params={"key": "nope"}).status_code == 403


async def test_stream_needs_the_key(watched_hub, monkeypatch):
    monkeypatch.setenv("ACCESS_KEY", "s3cret")

    missing = await main.stream(_request("/stream.mjpg"))
    wrong = await main.stream(_request("/stream.mjpg", query=b"key=nope"))

    assert missing.status_code == 401
    assert wrong.status_code == 403
    # A rejected request must never have taken a viewer slot or woken the
    # camera up.
    assert watched_hub.viewers == 0
    assert watched_hub.running is False


# ── /healthz ────────────────────────────────────────────────────────────────

def test_healthz_needs_no_key_and_starts_nothing(client, hub, monkeypatch):
    monkeypatch.setenv("ACCESS_KEY", "s3cret")

    response = client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["upstream"] == upstream_module.IDLE
    assert body["viewers"] == 0
    assert body["last_frame_age"] is None
    assert body["connections"] == 0
    # The whole point: a health check must not be a reason to open the camera.
    assert hub.running is False


def test_healthz_reports_a_live_upstream(client, hub):
    # Set by hand rather than via acquire(): this test is synchronous, so
    # there is no event loop for a reader task to be created on, and all
    # /healthz does is report counters.
    hub._viewers = 1
    hub._publish(JPEG)

    body = client.get("/healthz").json()

    assert body["viewers"] == 1
    assert body["last_frame_age"] is not None
    assert body["last_frame_age"] < 1.0
    assert body["max_viewers"] == settings.max_viewers()


def test_healthz_never_leaks_the_key(client, monkeypatch):
    monkeypatch.setenv("ACCESS_KEY", "s3cret")
    assert "s3cret" not in client.get("/healthz").text


# ── /snapshot.jpg ───────────────────────────────────────────────────────────

def test_snapshot_is_503_with_nothing_buffered(client, hub):
    response = client.get("/snapshot.jpg")
    assert response.status_code == 503
    assert response.json()["error"] == "no fresh frame"


def test_snapshot_serves_the_buffered_frame(client, hub):
    hub._publish(JPEG)
    response = client.get("/snapshot.jpg")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.content == JPEG


def test_snapshot_refuses_a_stale_frame(client, hub, monkeypatch):
    hub._publish(JPEG)
    monkeypatch.setattr(settings, "SNAPSHOT_MAX_AGE_SECONDS", 0.0)
    assert client.get("/snapshot.jpg").status_code == 503


def test_snapshot_does_not_start_the_upstream(client, hub):
    client.get("/snapshot.jpg")
    assert hub.running is False
    assert hub.connections == 0


# ── The viewer cap ──────────────────────────────────────────────────────────

async def test_max_viewers_is_enforced(watched_hub, monkeypatch):
    monkeypatch.setenv("MAX_VIEWERS", "2")

    first = await main.stream(_request("/stream.mjpg"))
    second = await main.stream(_request("/stream.mjpg"))
    third = await main.stream(_request("/stream.mjpg"))

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 503
    assert "already watching" in bytes(third.body).decode()
    assert watched_hub.viewers == 2
    # Two viewers, still one reader task between them.
    assert watched_hub.running is True

    # What Starlette does with a response: iterate the body, then run the
    # background task. Either one gives the slot back, and both together
    # must not give it back twice.
    for response in (first, second):
        await response.body_iterator.aclose()
        await response.background()  # the never-iterated backstop
    assert watched_hub.viewers == 0


async def test_a_slot_frees_up_when_a_viewer_leaves(watched_hub, monkeypatch):
    monkeypatch.setenv("MAX_VIEWERS", "1")

    first = await main.stream(_request("/stream.mjpg"))
    assert (await main.stream(_request("/stream.mjpg"))).status_code == 503

    await first.body_iterator.aclose()
    await first.background()
    assert watched_hub.viewers == 0

    second = await main.stream(_request("/stream.mjpg"))
    assert second.status_code == 200
    await second.body_iterator.aclose()
    await second.background()


# ── The fan-out itself ──────────────────────────────────────────────────────

async def test_fan_out_sends_well_formed_multipart_parts(watched_hub):
    watched_hub.acquire()
    parts = main._fan_out(main._Slot())
    watched_hub._publish(JPEG)

    part = await asyncio.wait_for(parts.__anext__(), timeout=2.0)

    assert part.startswith(f"--{main.BOUNDARY}\r\n".encode())
    assert b"Content-Type: image/jpeg\r\n" in part
    assert f"Content-Length: {len(JPEG)}\r\n".encode() in part
    assert part.endswith(JPEG + b"\r\n")
    await parts.aclose()


async def test_fan_out_skips_frames_rather_than_queueing_them(watched_hub, monkeypatch):
    """A slow viewer falls behind in time, not in memory, and gets the newest
    frame when it is ready - never a backlog of old ones."""
    monkeypatch.setenv("FPS", "2")  # one frame every 500ms
    monkeypatch.setattr(settings, "STALL_SECONDS", 5.0)
    watched_hub.acquire()
    parts = main._fan_out(main._Slot())

    watched_hub._publish(b"\xff\xd8first")
    assert b"first" in await asyncio.wait_for(parts.__anext__(), timeout=2.0)

    # Four frames arrive while this viewer is still rate-limited.
    for index in range(4):
        watched_hub._publish(b"\xff\xd8frame%d" % index)

    second = await asyncio.wait_for(parts.__anext__(), timeout=2.0)

    assert b"frame3" in second      # the latest
    assert b"frame0" not in second  # nothing was queued for it
    await parts.aclose()


async def test_fan_out_releases_its_slot_however_it_ends(watched_hub):
    watched_hub.acquire()
    parts = main._fan_out(main._Slot())
    watched_hub._publish(JPEG)
    await asyncio.wait_for(parts.__anext__(), timeout=2.0)
    assert watched_hub.viewers == 1

    await parts.aclose()

    assert watched_hub.viewers == 0


async def test_fan_out_gives_up_when_no_frame_ever_arrives(watched_hub, monkeypatch):
    """The viewer's response is ended rather than held open forever, which is
    what lets the page show its offline placeholder and retry."""
    monkeypatch.setattr(main, "_give_up_seconds", lambda: 0.2)
    watched_hub.acquire()
    parts = main._fan_out(main._Slot())

    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(parts.__anext__(), timeout=3.0)
    assert watched_hub.viewers == 0


async def test_fan_out_ignores_a_stale_cached_frame(watched_hub, monkeypatch):
    """A frame left over from the last time anyone watched is not a live view."""
    watched_hub._publish(JPEG)
    monkeypatch.setattr(settings, "STALL_SECONDS", 0.0)
    monkeypatch.setattr(main, "_give_up_seconds", lambda: 0.2)
    watched_hub.acquire()
    parts = main._fan_out(main._Slot())

    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(parts.__anext__(), timeout=3.0)


# ── Hardening ───────────────────────────────────────────────────────────────

def test_security_headers_on_every_response(client):
    for path in ("/", "/healthz", "/snapshot.jpg"):
        headers = client.get(path).headers
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["x-frame-options"] == "DENY"
        assert headers["referrer-policy"] == "no-referrer"
        assert headers["cache-control"] == "no-store"
        assert "default-src 'none'" in headers["content-security-policy"]
        assert "frame-ancestors 'none'" in headers["content-security-policy"]
        # Nothing is meant to embed this page from another origin.
        assert "access-control-allow-origin" not in headers


def test_no_cors_even_when_asked_nicely(client):
    response = client.get("/healthz", headers={"Origin": "https://evil.test"})
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize("path", [
    "/api/camera_proxy/camera.other",
    "/camera_proxy_stream/camera.other",
    "/states/sensor.anything",
    "/index.html",
    "/favicon.ico",
    "/stream.mjpg/extra",
    "/snapshot.jpg.map",
])
def test_there_are_no_other_routes(client, path):
    """This app proxies one camera and nothing else. No Home Assistant
    endpoint, no other entity, no static tree."""
    assert client.get(path).status_code in (404, 405)


def test_no_api_docs_are_exposed(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_the_page_never_contains_the_supervisor_token(client, monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "super-secret-token")
    for path in ("/", "/healthz"):
        assert "super-secret-token" not in client.get(path).text
