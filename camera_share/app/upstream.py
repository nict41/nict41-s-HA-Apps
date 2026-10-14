"""The single subscription to Home Assistant, and the fan-out behind it.

The printer camera this app exists for accepts exactly **one** connection and
its firmware falls over when something tries to open a second. The Elegoo
integration (>= 2.12.3.0) owns that one connection in its own frame hub and
fans frames out to Home Assistant consumers; this module's job is to be
exactly one of those consumers, no matter how many people load the page:

    HA camera_proxy_stream  (one subscription, only while >= 1 viewer)
       |
       +-> latest frame in memory
             +-> viewer 1   (MJPEG, per-viewer fps cap)
             +-> viewer 2
             +-> ...

The rules that follow from that, all of them load-bearing:

* One reader task, ever. Viewers take a slot; the first one starts the task
  and the last one leaving stops it after a short idle delay (so a page
  reload doesn't tear the subscription down and rebuild it).
* Frames are never queued per viewer. Each viewer reads whatever the latest
  frame is when it is ready to send one; a slow client falls behind in time,
  not in memory, and cannot hold anything up for anybody else.
* A stalled stream is the normal failure. When the printer's camera server
  dies the HTTP response stays open and simply goes quiet, so silence is the
  only symptom: no frame for STALL_SECONDS closes the subscription.
* Reconnects back off exponentially with jitter, 2 s to 60 s. A tight retry
  loop against a one-connection camera is how its firmware was crashed
  before, so there is no code path here that retries without sleeping.
"""

import asyncio
import logging
import random
import time

import httpx

import settings
from mjpeg import MjpegError, MjpegParser, boundary_from_content_type

_LOG = logging.getLogger("camera_share.upstream")

IDLE = "idle"
CONNECTING = "connecting"
LIVE = "live"
OFFLINE = "offline"


class UpstreamError(Exception):
    """The subscription failed in a way that warrants a backed-off retry."""


class Upstream:
    """Holds at most one HA camera subscription and the latest frame from it."""

    def __init__(self, client: httpx.AsyncClient | None = None):
        self._client = client
        self._reader: asyncio.Task | None = None
        self._idle_task: asyncio.Task | None = None
        self._viewers = 0
        self._state = IDLE
        self._error: str | None = None

        # The latest frame, and a per-frame sequence number viewers wait on.
        # `_wakeup` is swapped for a fresh Event on every frame so that a
        # viewer which arrives mid-frame can't miss the edge.
        self._frame: bytes | None = None
        self._seq = 0
        self._frame_at: float | None = None
        self._wakeup = asyncio.Event()

        # Diagnostics, and what the tests assert on: how many times a stream
        # was actually opened against Home Assistant.
        self.connections = 0

    # ── Observable state ────────────────────────────────────────────────────

    @property
    def state(self) -> str:
        return self._state

    @property
    def viewers(self) -> int:
        return self._viewers

    @property
    def seq(self) -> int:
        return self._seq

    @property
    def frame(self) -> bytes | None:
        return self._frame

    @property
    def running(self) -> bool:
        return self._reader is not None and not self._reader.done()

    def frame_age(self) -> float | None:
        """Seconds since the last frame, or None if there has never been one."""
        if self._frame_at is None:
            return None
        return max(0.0, time.monotonic() - self._frame_at)

    def status(self) -> dict:
        """A snapshot for /healthz. Starts nothing and touches no camera."""
        age = self.frame_age()
        return {
            "upstream": self._state,
            "upstream_running": self.running,
            "viewers": self._viewers,
            "max_viewers": settings.max_viewers(),
            "last_frame_age": None if age is None else round(age, 2),
            "connections": self.connections,
            "camera_entity": settings.camera_entity(),
            "error": self._error,
        }

    def fresh_frame(self) -> bytes | None:
        """The latest frame if it is recent enough to serve, else None."""
        age = self.frame_age()
        if self._frame is None or age is None or age > settings.SNAPSHOT_MAX_AGE_SECONDS:
            return None
        return self._frame

    # ── Viewer slots ────────────────────────────────────────────────────────

    def full(self) -> bool:
        return self._viewers >= settings.max_viewers()

    def acquire(self) -> None:
        """Take a viewer slot, starting the subscription if it isn't running."""
        self._viewers += 1
        if self._idle_task is not None:
            self._idle_task.cancel()
            self._idle_task = None
        if not self.running:
            self._reader = asyncio.create_task(self._run(), name="camera_share.upstream")

    def release(self) -> None:
        """Give a viewer slot back, scheduling the idle shutdown if it was the
        last one. Never stops the subscription immediately: a reload or a
        phone switching from wifi to mobile shouldn't cost a reconnect."""
        self._viewers = max(0, self._viewers - 1)
        if self._viewers == 0 and self._idle_task is None:
            try:
                self._idle_task = asyncio.create_task(
                    self._idle_stop(), name="camera_share.idle")
            except RuntimeError:
                # Released from somewhere with no running loop. Both callers
                # are on the loop, so this is defence in depth rather than a
                # path that is expected: say so rather than take the app down
                # over a viewer leaving.
                _LOG.warning("Could not schedule the idle shutdown - no event loop")

    async def _idle_stop(self) -> None:
        try:
            await asyncio.sleep(settings.IDLE_SHUTDOWN_SECONDS)
        except asyncio.CancelledError:
            return
        if self._viewers == 0:
            _LOG.info("No viewers for %.0fs - releasing the camera subscription",
                      settings.IDLE_SHUTDOWN_SECONDS)
            await self.stop()
        self._idle_task = None

    async def stop(self) -> None:
        """Close the subscription now. Safe to call when nothing is running.

        The state goes first. `running` turns False the moment the reader is
        detached, so setting the state afterwards leaves a window where this
        reads as "not running, still live" - which is exactly what /healthz
        and the page would show while the subscription is winding down.
        """
        self._state = IDLE
        reader, self._reader = self._reader, None
        if reader is not None and not reader.done():
            reader.cancel()
            try:
                await reader
            except asyncio.CancelledError:
                pass

    async def aclose(self) -> None:
        idle, self._idle_task = self._idle_task, None
        if idle is not None:
            idle.cancel()
        await self.stop()
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ── Frame delivery ──────────────────────────────────────────────────────

    async def wait_frame(self, since_seq: int, timeout: float) -> bool:
        """Wait until a frame newer than `since_seq` exists.

        Returns True if one is available, False on timeout. Callers always
        read `.frame`/`.seq` afterwards rather than being handed a frame, so
        nothing is buffered on their behalf and a slow viewer simply misses
        the frames it wasn't ready for.
        """
        if self._seq != since_seq:
            return True
        waiter = self._wakeup
        try:
            await asyncio.wait_for(waiter.wait(), timeout)
        except asyncio.TimeoutError:
            return False
        return self._seq != since_seq

    def _publish(self, frame: bytes) -> None:
        self._frame = frame
        self._frame_at = time.monotonic()
        self._seq += 1
        wakeup, self._wakeup = self._wakeup, asyncio.Event()
        wakeup.set()

    # ── The one subscription ────────────────────────────────────────────────

    async def _run(self) -> None:
        """Keep one subscription alive for as long as there are viewers."""
        backoff = settings.BACKOFF_START_SECONDS
        try:
            while True:
                try:
                    produced = await self._read_stream()
                    if produced:
                        backoff = settings.BACKOFF_START_SECONDS
                    self._error = None
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - every failure retries
                    self._error = str(exc)
                    _LOG.warning("Camera subscription failed: %s", exc)

                self._state = OFFLINE
                delay = backoff * (1.0 + random.uniform(0.0, settings.BACKOFF_JITTER))
                _LOG.info("Retrying the camera subscription in %.1fs", delay)
                await asyncio.sleep(delay)
                backoff = min(backoff * 2.0, settings.BACKOFF_MAX_SECONDS)
        except asyncio.CancelledError:
            self._state = IDLE
            raise

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            # No overall timeout: the response is a stream that is meant to
            # stay open. Silence is caught by the stall watchdog below, which
            # is explicit rather than left to the transport.
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(None, connect=10.0,
                                      read=settings.STALL_SECONDS * 2),
            )
        return self._client

    async def _read_stream(self) -> bool:
        """Open one subscription and pump frames until it ends or stalls.

        Returns True if at least one frame arrived (which resets the
        backoff). Raises UpstreamError on anything retryable.
        """
        token = settings.supervisor_token()
        if not token:
            raise UpstreamError(
                "no SUPERVISOR_TOKEN - the app can't reach the Home Assistant API"
            )

        entity = settings.camera_entity()
        url = f"{settings.ha_api()}/camera_proxy_stream/{entity}"
        self._state = CONNECTING
        produced = False

        self.connections += 1
        _LOG.info("Subscribing to %s at up to %d fps (subscription #%d)",
                  entity, settings.fps(), self.connections)

        async with self._http().stream(
            "GET",
            url,
            params={"fps": settings.fps()},
            headers={"Authorization": f"Bearer {token}"},
        ) as response:
            if response.status_code != 200:
                await response.aread()
                raise UpstreamError(
                    f"Home Assistant returned {response.status_code} for {entity} - "
                    "check the camera entity id and that the Elegoo integration "
                    "is at least 2.12.3.0"
                )

            boundary = boundary_from_content_type(response.headers.get("content-type"))
            if boundary is None:
                raise UpstreamError(
                    "the camera stream has no multipart boundary in its "
                    f"Content-Type ({response.headers.get('content-type')!r})"
                )

            parser = MjpegParser(boundary)
            chunks = response.aiter_bytes().__aiter__()

            while True:
                try:
                    chunk = await asyncio.wait_for(
                        chunks.__anext__(), timeout=settings.STALL_SECONDS
                    )
                except StopAsyncIteration:
                    _LOG.info("Camera stream ended after %s frame(s)", self._seq)
                    return produced
                except asyncio.TimeoutError as exc:
                    raise UpstreamError(
                        f"no data for {settings.STALL_SECONDS:.0f}s - the printer's "
                        "camera server has most likely stopped sending"
                    ) from exc

                try:
                    frames = parser.feed(chunk)
                except MjpegError as exc:
                    raise UpstreamError(str(exc)) from exc

                for frame in frames:
                    self._publish(frame)
                    produced = True
                    self._state = LIVE

                if parser.closed:
                    return produced

                # Data still arriving but no whole frame in STALL_SECONDS is
                # also a stall - keepalive bytes from a dead camera count as
                # silence, not as liveness.
                age = self.frame_age()
                if produced and age is not None and age > settings.STALL_SECONDS:
                    raise UpstreamError(
                        f"no frame for {age:.0f}s despite an open stream"
                    )
