"""Pushing rendered pictures to the clock over HTTP.

POST http://<clock>/panel?tube=N with raw RGB565 big-endian body. Only tubes
whose bytes changed (hash) are sent, plus any tube in the clock's
`panels_needed` bitmask (sent immediately, even if unchanged). 409 means the
clock is not in a panel mode -> re-publish display=4 once and retry; 503 /
connection errors retry with backoff (max ~10 s). Tubes are sent one at a
time - the clock accepts one upload at a time.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Awaitable, Callable

import aiohttp

from rgb565 import TUBE_COUNT, TUBE_H, TUBE_W

_LOG = logging.getLogger(__name__)

DISPLAY_PANELS = 4
DISPLAY_CLOCK = 0
MAX_BACKOFF = 10.0

PublishDisplay = Callable[[int], Awaitable[None]]


class ClockPushError(Exception):
    def __init__(self, status: int | None, message: str) -> None:
        super().__init__(message)
        self.status = status


class ClockPusher:
    """Sends tube payloads to the clock, tracking what was last sent."""

    def __init__(self, session: aiohttp.ClientSession, base_url: str,
                 user: str = "", password: str = "",
                 publish_display: PublishDisplay | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self._session = session
        self._base = base_url.rstrip("/")
        self._auth = aiohttp.DigestAuth(user, password) if user else None
        self._publish_display = publish_display
        self._sleep = sleep
        self._last_hash: list[str | None] = [None] * TUBE_COUNT
        self._last_bytes: list[bytes | None] = [None] * TUBE_COUNT
        self._lock = asyncio.Lock()  # one upload at a time
        self._backoff = 1.0
        self.last_error: str | None = None
        self.last_status: dict | None = None  # last GET /panel answer
        # False after a send gave up for connection reasons (clock unreachable);
        # True again after any successful upload or probe
        self.reachable = True

    def reset(self) -> None:
        """Forget what was sent (e.g. after the clock rebooted)."""
        self._last_hash = [None] * TUBE_COUNT
        self._last_bytes = [None] * TUBE_COUNT

    async def probe(self) -> bool:
        """GET /panel to see whether the clock answers at all. Sets `reachable`."""
        try:
            async with self._session.get(f"{self._base}/panel", auth=self._auth,
                                         timeout=aiohttp.ClientTimeout(total=10)) as resp:
                ok = resp.status == 200
                if ok:
                    try:
                        self.last_status = await resp.json(content_type=None)
                    except Exception:
                        self.last_status = None
        except Exception as err:
            _LOG.debug("GET /panel probe failed: %s", err)
            ok = False
        self.reachable = ok
        if ok:
            self.last_error = None
            self._backoff = 1.0
        return ok

    @staticmethod
    def _hash(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def tubes_to_send(self, tube_bytes: list[bytes], needed_mask: int = 0) -> list[int]:
        """Indices of tubes whose bytes changed, plus any tube in needed_mask."""
        out = []
        for i, data in enumerate(tube_bytes):
            h = self._hash(data)
            if needed_mask & (1 << i) or self._last_hash[i] != h:
                out.append(i)
        return out

    async def fetch_panel_info(self) -> dict | None:
        """GET /panel -> {"tubes": [...], "needed": [...], "width": ..., "height": ...}."""
        try:
            async with self._session.get(f"{self._base}/panel", auth=self._auth,
                                         timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                import json
                self.last_status = await resp.json(content_type=None)
                return self.last_status
        except Exception as err:
            _LOG.debug("GET /panel failed: %s", err)
            return None

    async def send_tubes(self, tube_bytes: list[bytes], needed_mask: int = 0) -> list[int]:
        """Send the tubes that changed (plus needed ones). Returns sent indices.

        When a tube upload fails with a connection error/timeout, the remaining
        tubes of this call are skipped: the clock is unreachable, and retrying
        6 tubes x 6 times would block the app for minutes."""
        todo = self.tubes_to_send(tube_bytes, needed_mask)
        sent: list[int] = []
        for i in todo:
            ok, connection_lost = await self._send_one(i, tube_bytes[i])
            if ok:
                sent.append(i)
            elif connection_lost:
                self.reachable = False
                _LOG.warning("Clock unreachable while sending tube %d - skipping the remaining %d tube(s) of this send",
                             i, len(todo) - len(sent) - 1)
                break
        return sent

    async def _send_one(self, tube: int, data: bytes) -> tuple[bool, bool]:
        """Upload one tube. Returns (ok, connection_lost)."""
        ok, connection_lost = await self._post_panel({"tube": tube}, data)
        if ok:
            h = self._hash(data)
            self._last_hash[tube] = h
            self._last_bytes[tube] = data
        return ok, connection_lost

    async def send_tube(self, tube: int, data: bytes) -> bool:
        """Upload one whole tube unconditionally, even if its bytes are unchanged
        (used by screen transitions). Returns True on success."""
        ok, connection_lost = await self._send_one(tube, data)
        if connection_lost:
            self.reachable = False
        return ok

    async def send_rect(self, tube: int, x: int, y: int, w: int, h: int, data: bytes) -> bool:
        """Upload a rectangular region of one tube (POST /panel?tube=N&x=X&y=Y&w=W&h=H,
        raw RGB565 big-endian body of w*h*2 bytes). On success the region is patched
        into the tube's last bytes (when known) and its hash recomputed, so a later
        full refresh only resends the tube if the whole picture really differs.
        Returns True on success."""
        if not (0 <= tube < TUBE_COUNT):
            raise ValueError(f"tube must be 0..{TUBE_COUNT - 1}, got {tube}")
        if not (0 <= x and 0 <= y and x + w <= TUBE_W and y + h <= TUBE_H and w > 0 and h > 0):
            raise ValueError(f"rect ({x},{y},{w},{h}) outside the {TUBE_W}x{TUBE_H} tube")
        if len(data) != w * h * 2:
            raise ValueError(f"rect body must be {w * h * 2} bytes (w*h*2), got {len(data)}")
        ok, connection_lost = await self._post_panel(
            {"tube": tube, "x": x, "y": y, "w": w, "h": h}, data)
        if connection_lost:
            self.reachable = False
        if not ok:
            return False
        last = self._last_bytes[tube]
        if last is None or len(last) != TUBE_W * TUBE_H * 2:
            # the tube's full picture is unknown: a later full refresh must compare
            # against nothing, so its hash is unknown too
            self._last_hash[tube] = None
        else:
            row_bytes = TUBE_W * 2
            patched = bytearray(last)
            for row in range(h):
                off = ((y + row) * TUBE_W + x) * 2
                patched[off:off + w * 2] = data[row * w * 2:(row + 1) * w * 2]
            self._last_bytes[tube] = bytes(patched)
            self._last_hash[tube] = self._hash(self._last_bytes[tube])
        return True

    async def _post_panel(self, params: dict, data: bytes) -> tuple[bool, bool]:
        """POST /panel with the given query params and body, with the shared
        retry/409/503/unreachable handling. Returns (ok, connection_lost)."""
        tube = params.get("tube")
        async with self._lock:
            for attempt in range(6):
                try:
                    async with self._session.post(
                        f"{self._base}/panel", params=params,
                        data=data, auth=self._auth,
                        headers={"Content-Type": "application/octet-stream"},
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as resp:
                        if resp.status == 200:
                            self._backoff = 1.0
                            self.last_error = None
                            self.reachable = True
                            return True, False
                        if resp.status == 409:
                            # clock not in a panel mode: re-publish display=4 once, retry
                            _LOG.warning("Clock returned 409 (not a panel tube) on tube %s - "
                                         "re-publishing display=4", tube)
                            if self._publish_display is not None:
                                try:
                                    await self._publish_display(DISPLAY_PANELS)
                                except Exception as err:
                                    _LOG.warning("Could not publish display=4: %s", err)
                            await self._sleep(1.0)
                            continue
                        if resp.status == 503:
                            _LOG.debug("Clock busy (503) on tube %s - retrying", tube)
                            await self._sleep(self._backoff)
                            self._backoff = min(self._backoff * 2, MAX_BACKOFF)
                            continue
                        body = await resp.text()
                        self.last_error = f"tube {tube}: HTTP {resp.status}: {body[:120]}"
                        _LOG.warning("POST /panel tube %s -> %d %s", tube, resp.status, body[:120])
                        return False, False
                except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                    self.last_error = f"tube {tube}: {err}"
                    _LOG.debug("POST /panel tube %s failed: %s - retrying", tube, err)
                    await self._sleep(self._backoff)
                    self._backoff = min(self._backoff * 2, MAX_BACKOFF)
            self.last_error = f"tube {tube}: giving up after retries ({self.last_error})"
            _LOG.error("Could not send tube %s to the clock: %s", tube, self.last_error)
            return False, True
