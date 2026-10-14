"""Camera Share: one Home Assistant camera, served as a read-only public page.

Four routes and nothing else. This page is meant to be published on the
internet through a Cloudflare tunnel, so every request is treated as hostile:
nothing here proxies any other Home Assistant endpoint or entity, no request
can name a different camera, and the Supervisor token never leaves the
container.

    GET /               the page
    GET /stream.mjpg    the live MJPEG fan-out (takes a viewer slot)
    GET /snapshot.jpg   the latest buffered frame, if it is fresh
    GET /healthz        status as JSON - starts nothing, needs no camera

The camera itself is handled by `upstream.py`, which holds exactly one
subscription to Home Assistant no matter how many people are watching. See
its docstring for why that matters.
"""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

import settings
import upstream as upstream_module

_LOG = logging.getLogger("camera_share")

APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

# The boundary this app sends to *its* viewers. Nothing to do with the
# boundary Home Assistant sends us, which is read from the response header.
BOUNDARY = "camsharepart"


def _give_up_seconds() -> float:
    """How long a viewer's stream is held open with nothing sendable before it
    is ended. The page notices the stream stopped, shows the offline
    placeholder and retries, recovering by itself when frames return."""
    return settings.STALL_SECONDS + 5.0


# Headers on every response. No CORS header is ever sent - nothing is meant
# to embed this - and the CSP allows only what the page itself needs.
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    # The share link carries the access key in its query string, so no
    # referrer may ever leave this page.
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "geolocation=(), camera=(), microphone=()",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; "
        "script-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'"
    ),
    "Cache-Control": "no-store",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    _LOG.info("Camera Share starting. Camera: %s | up to %d fps per viewer | "
              "max %d viewers", settings.camera_entity(), settings.fps(),
              settings.max_viewers())
    if not settings.auth_required():
        # Three lines rather than one: a single warning scrolls past in a
        # busy app log, and this is the one that decides whether the camera
        # is public to anyone who learns the hostname.
        _LOG.warning("=" * 72)
        _LOG.warning("NO ACCESS KEY IS SET - anyone who reaches this port can "
                     "watch the camera.")
        _LOG.warning("Set the 'access_key' option BEFORE pointing a Cloudflare "
                     "tunnel at this app.")
        _LOG.warning("=" * 72)
    if not settings.supervisor_token():
        _LOG.error("No SUPERVISOR_TOKEN in the environment - the app cannot "
                   "reach the Home Assistant API and will show the camera as "
                   "offline. Is 'homeassistant_api: true' set?")
    yield
    await upstream.aclose()


app = FastAPI(title="Camera Share", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)

upstream = upstream_module.Upstream()


class SecurityHeadersMiddleware:
    """Adds the security headers to every response.

    Pure ASGI on purpose. Starlette's `BaseHTTPMiddleware` (what the
    `@app.middleware("http")` decorator builds) wraps the request's `receive`
    and runs the whole response inside its own task group, which is a known
    source of trouble for long-lived streaming responses and their background
    tasks - and this app's main route is a response that stays open for as
    long as someone is watching. Editing the `http.response.start` message
    leaves the stream path completely alone.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in _SECURITY_HEADERS.items():
                    if name not in headers:
                        headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)


app.add_middleware(SecurityHeadersMiddleware)


# ── Authorisation ───────────────────────────────────────────────────────────

_OK = "ok"
_MISSING = "missing"
_BAD = "bad"


def _verdict(request: Request) -> str:
    """Whether this request may see the camera: _OK, _MISSING or _BAD.

    _MISSING means no credential was presented at all (401); _BAD means one
    was and it was wrong (403). Both render the same page - the distinction
    is for the status code and the log, not for the visitor.
    """
    if not settings.auth_required():
        return _OK
    presented = request.query_params.get("key")
    if presented is not None:
        return _OK if settings.key_matches(presented) else _BAD
    cookie = request.cookies.get(settings.COOKIE_NAME)
    if cookie is not None:
        return _OK if settings.cookie_matches(cookie) else _BAD
    return _MISSING


def _apply_cookie(request: Request, response: Response) -> None:
    """Remember a correct `?key=` so a plain reload works without it.

    `secure` is set whenever the request arrived over HTTPS, which through a
    Cloudflare tunnel it always does (the tunnel terminates TLS and sends
    X-Forwarded-Proto: https). It is dropped for a plain-HTTP request only
    because browsers refuse a Secure cookie from a non-secure origin, which
    would otherwise make the app unusable on the LAN - and on the LAN there
    is no transport confidentiality to protect anyway. The page also carries
    the key in its own sub-resource URLs, so it still works for a viewer who
    blocks cookies entirely.
    """
    if not settings.auth_required():
        return
    if request.query_params.get("key") is None:
        return
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    https = forwarded == "https" or request.url.scheme == "https"
    response.set_cookie(
        settings.COOKIE_NAME,
        settings.cookie_value(),
        max_age=30 * 24 * 3600,
        httponly=True,
        secure=https,
        samesite="lax",
        path="/",
    )


def _denied(verdict: str) -> HTMLResponse:
    status = 401 if verdict == _MISSING else 403
    _LOG.warning("Denied a viewer: %s access key", verdict)
    return HTMLResponse(
        _notice_page(
            "Private camera",
            "This camera needs an access key. Ask whoever shared the link for "
            "the full address, including the <code>?key=</code> part.",
        ),
        status_code=status,
    )


# ── Routes ──────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    verdict = _verdict(request)
    if verdict != _OK:
        return _denied(verdict)

    # An authorised viewer already holds the key, so the page's own stream
    # and snapshot URLs carry it too. That keeps the page working when the
    # cookie is refused (private browsing, cookie-blocking extensions).
    suffix = f"?key={quote(settings.access_key(), safe='')}" if settings.auth_required() else ""
    response = templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "page_title": settings.page_title(),
            "stream_url": f"stream.mjpg{suffix}",
        },
    )
    _apply_cookie(request, response)
    return response


@app.get("/stream.mjpg")
async def stream(request: Request):
    verdict = _verdict(request)
    if verdict != _OK:
        return _denied(verdict)

    # Checked and claimed with no await in between, so two simultaneous
    # viewers can't both pass a cap of one.
    if upstream.full():
        _LOG.info("Refused a viewer: %d already watching", upstream.viewers)
        return HTMLResponse(
            _notice_page(
                "Camera busy",
                "The maximum number of people are already watching this "
                "camera. Try again in a minute.",
            ),
            status_code=503,
        )
    upstream.acquire()
    slot = _Slot()

    response = StreamingResponse(
        _fan_out(slot),
        media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
        # The generator's own `finally` is what normally gives the slot back.
        # This is the backstop for a response that is never iterated at all,
        # so a viewer slot can't be stranded by a client that vanishes
        # between the route returning and the body being sent. It has to be
        # a coroutine: Starlette runs a plain callable in a worker thread,
        # where there is no event loop to schedule the idle shutdown on.
        background=BackgroundTask(_release_slot, slot),
    )
    _apply_cookie(request, response)
    return response


@app.get("/snapshot.jpg")
async def snapshot(request: Request):
    """The latest frame from this app's own buffer, if it is fresh.

    Deliberately does not start the subscription: a snapshot is a cheap read
    of what the fan-out already has, never a reason to open the camera. With
    nobody watching there is nothing to serve, which is a 503.
    """
    verdict = _verdict(request)
    if verdict != _OK:
        return _denied(verdict)

    frame = upstream.fresh_frame()
    if frame is None:
        return JSONResponse(
            {"error": "no fresh frame", "upstream": upstream.state},
            status_code=503,
        )
    response = Response(content=frame, media_type="image/jpeg")
    _apply_cookie(request, response)
    return response


@app.get("/healthz")
async def healthz():
    """Status only. Needs no access key, touches no camera, starts nothing."""
    return JSONResponse({"ok": True, **upstream.status()})


# ── Fan-out ─────────────────────────────────────────────────────────────────

class _Slot:
    """One viewer's claim on the fan-out, given back exactly once.

    Both the body generator and the response's background task release it,
    whichever happens, and neither can double-count.
    """

    __slots__ = ("_released",)

    def __init__(self):
        self._released = False

    def release(self) -> None:
        if not self._released:
            self._released = True
            upstream.release()


async def _release_slot(slot: "_Slot") -> None:
    slot.release()


async def _fan_out(slot: "_Slot"):
    """One viewer's MJPEG stream, rate-limited and frame-skipping.

    Frames are never queued for a viewer: each pass reads whatever the latest
    frame is, so a slow client falls behind in time rather than in memory and
    can't hold up anyone else. The viewer slot is released in `finally`,
    whatever ends the response - a closed tab, a dead tunnel, a cancellation.
    """
    interval = 1.0 / float(settings.fps())
    seen = 0
    last_sent = time.monotonic()
    try:
        while True:
            fresh = await upstream.wait_frame(seen, timeout=1.0)
            # Advance past whatever is current whether or not it gets sent.
            # Not doing so is a spin: wait_frame returns immediately while a
            # frame it won't send is the newest one there is.
            seen = upstream.seq
            if fresh:
                frame = upstream.frame
                age = upstream.frame_age()
                if frame is not None and age is not None and age <= settings.STALL_SECONDS:
                    yield _part(frame)
                    last_sent = time.monotonic()
                    # The per-viewer cap. Frames that arrive during this sleep
                    # are skipped, not buffered.
                    await asyncio.sleep(interval)
                    continue
            give_up = _give_up_seconds()
            if time.monotonic() - last_sent > give_up:
                _LOG.info("Ending a viewer's stream: no frame for %.0fs", give_up)
                return
    except asyncio.CancelledError:
        raise
    finally:
        slot.release()


def _part(frame: bytes) -> bytes:
    return (
        f"--{BOUNDARY}\r\n"
        "Content-Type: image/jpeg\r\n"
        f"Content-Length: {len(frame)}\r\n\r\n"
    ).encode("ascii") + frame + b"\r\n"


def _notice_page(heading: str, message: str) -> str:
    """A self-contained page for the cases where there is no camera to show.

    Inline rather than templated: these are served on paths that may be hit
    by anything on the internet, and they must not depend on the rest of the
    app being in a working state.
    """
    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{heading}</title><style>"
        ":root{color-scheme:light dark}"
        "body{margin:0;min-height:100vh;display:grid;place-items:center;"
        "background:#0c0e09;color:#e7ead9;font:15px/1.5 \"Helvetica Neue\","
        "Helvetica,Arial,sans-serif;padding:1.5rem}"
        "main{max-width:26rem;text-align:center}"
        "h1{font-size:.8rem;letter-spacing:.16em;text-transform:uppercase;"
        "margin:0 0 .75rem}"
        "p{color:#9ba189;margin:0}"
        "code{font-family:ui-monospace,Menlo,Consolas,monospace;color:#c6f53c}"
        "</style></head><body><main>"
        f"<h1>{heading}</h1><p>{message}</p>"
        "</main></body></html>"
    )
