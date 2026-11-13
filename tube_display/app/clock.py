"""Pushing rendered pictures to the clock over HTTP.

POST http://<clock>/panel?tube=N with a raw RGB565 big-endian body (old firmware),
or a QOI / JPEG body with &format=qoi|jpeg (HA Tubes 2.4+, advertised as "formats"
in GET /panel). Only tubes whose RGB565 bytes changed (hash) are sent, plus any
tube in the clock's `panels_needed` bitmask (sent immediately, even if unchanged).
409 means the clock is not in a panel mode -> re-publish display=4 once and retry;
503 / connection errors retry with backoff (max ~10 s). Tubes are sent one at a
time - the clock accepts one upload at a time. Every POST body goes out with
Content-Type: application/octet-stream (the firmware parses form content types as
parameters).

GIFs (HA Tubes 2.4+, "gif": true in GET /panel): POST /gif?name=<name>.gif uploads
a file (max 600 KB, 135x240), POST /gif/play?tube=N&name=... starts it on a tube,
POST /gif/stop[?tube=N] stops it, DELETE /gif?name= removes it, GET /gif lists the
files. A /panel upload to a tube stops its GIF.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import re
import struct
import time
from collections import OrderedDict
from typing import Awaitable, Callable

import aiohttp

from rgb565 import TUBE_COUNT, TUBE_H, TUBE_W, rgb565_to_image

try:
    from PIL import Image
except Exception:  # Pillow not importable (bare test env): raw RGB565 still works
    Image = None

_LOG = logging.getLogger(__name__)

DISPLAY_PANELS = 4
DISPLAY_CLOCK = 0
MAX_BACKOFF = 10.0
# Largest POST /panel/batch body when the clock can't hold the batch in memory and draws it as it
# arrives: decoding on its network task loses packets, so a bigger body stalls for 0.4 s or more.
BATCH_MAX_BYTES = 3000
JPEG_QUALITY = 88
GIF_MAX_BYTES = 600 * 1024
GIF_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,36}\.gif$")   # at most 40 chars incl. ".gif"
GIF_TD_PREFIX = "td-"     # prefix for files this app uploads (never the user's own)
# encoded batch records are cached (tube, sha256 of the rgb565 bytes, x, y, w, h, format,
# held) so an unchanged tube is not QOI/JPEG-encoded again; small LRU, cleared by reset()
BATCH_RECORD_CACHE_MAX = 24

PublishDisplay = Callable[[int], Awaitable[None]]


def gif_clock_name(name: str) -> str:
    """The name a picture-library GIF gets on the clock: prefixed "td-" (so it never
    clashes with files the user put there), charset-restricted, at most 40 chars."""
    stem = name[:-4] if name.lower().endswith(".gif") else name
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", stem)
    return ("td-" + stem)[:36] + ".gif"


def _encode_qoi(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="QOI")
    return buf.getvalue()


def _encode_jpeg(img, quality: int = JPEG_QUALITY) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def _retry_after(resp) -> float | None:
    """Seconds to wait from the X-Retry-After-Ms header of a 503 (the clock is still
    drawing a batch), or None without one."""
    v = (getattr(resp, "headers", None) or {}).get("X-Retry-After-Ms")
    try:
        ms = int(v)
    except (TypeError, ValueError):
        return None
    return min(max(ms, 10), 3000) / 1000.0


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
        # capabilities learned from GET /panel (HA Tubes 2.4+ advertises them)
        self.formats: set[str] = set()       # e.g. {"rgb565", "qoi", "jpeg"}
        self.gif_supported = False           # "gif": true in GET /panel
        self.batch_supported = False         # "batch": true (HA Tubes 2.5+: POST /panel/batch)
        # HA Tubes 2.5+: the largest batch the clock can hold in memory now (it then draws it on
        # a task of its own, and can play a transition with it), and the transitions it plays
        self.batch_buffer = 0
        self.transitions: set[str] = set()
        self._caps_known = False
        self._qoi_ok = True                  # False after a 400 on a QOI upload
        self._qoi_400_logged = False
        self._playing: set[int] = set()      # tubes whose GIF is playing (per /panel)
        self._gif_uploaded: dict[str, int] = {}   # name -> size of files we uploaded
        self._sent_log: list[tuple[float, int]] = []   # (monotonic, body bytes) per POST
        self._record_cache: OrderedDict[tuple, bytes] = OrderedDict()   # encoded batch records

    def reset(self) -> None:
        """Forget what was sent (e.g. after the clock rebooted). The capabilities
        are re-learned from the clock on the next use (it may run new firmware)."""
        self._last_hash = [None] * TUBE_COUNT
        self._last_bytes = [None] * TUBE_COUNT
        self._caps_known = False
        self.formats = set()
        self.gif_supported = False
        self.batch_supported = False
        self.batch_buffer = 0
        self.transitions = set()
        self._qoi_ok = True
        self._playing = set()
        self._record_cache.clear()

    # ------------------------------------------------------------------
    # capabilities (GET /panel)
    # ------------------------------------------------------------------

    def _learn_caps(self, info: dict | None) -> None:
        """Cache `formats` / `gif` / `playing` from a GET /panel answer."""
        if isinstance(info, dict):
            fmts = info.get("formats")
            self.formats = {str(f) for f in fmts} if isinstance(fmts, list) else set()
            self.gif_supported = bool(info.get("gif"))
            self.batch_supported = bool(info.get("batch"))
            buf = info.get("batch_buffer")
            self.batch_buffer = int(buf) if isinstance(buf, (int, float)) and not isinstance(buf, bool) else 0
            tr = info.get("transitions")
            self.transitions = {str(t) for t in tr} if isinstance(tr, list) else set()
            playing = info.get("playing")
            if isinstance(playing, list):
                self._playing = {int(t) for t in playing
                                 if isinstance(t, (int, float)) and not isinstance(t, bool)}
        else:
            self.formats = set()
            self.gif_supported = False
            self.batch_supported = False
            self.batch_buffer = 0
            self.transitions = set()

    async def _fetch_caps(self) -> None:
        """GET /panel once and cache the clock's capabilities (formats, gif)."""
        try:
            async with self._session.get(f"{self._base}/panel", auth=self._auth,
                                         timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return
                try:
                    info = await resp.json(content_type=None)
                except Exception:
                    info = None
        except Exception as err:
            _LOG.debug("GET /panel (capabilities) failed: %s", err)
            return
        self.last_status = info if isinstance(info, dict) else None
        self._learn_caps(self.last_status)
        self._caps_known = True

    async def _ensure_caps(self) -> None:
        if not self._caps_known:
            await self._fetch_caps()

    @property
    def upload_format(self) -> str:
        """The body format used for uploads: "qoi" when the clock takes QOI, else "rgb565"."""
        return "qoi" if ("qoi" in self.formats and self._qoi_ok and Image is not None) else "rgb565"

    def bytes_per_minute(self) -> int:
        """Rolling count of POST body bytes sent to the clock over the last 60 s."""
        now = time.monotonic()
        self._sent_log = [(t, n) for t, n in self._sent_log if now - t < 60.0]
        return sum(n for _, n in self._sent_log)

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
            self._learn_caps(self.last_status)
            self._caps_known = True
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
        """GET /panel -> {"tubes": [...], "needed": [...], "width": ..., "height": ...,
        and on HA Tubes 2.4+: "formats", "gif", "playing", ...}. Also caches the
        capabilities. Returns None when the clock does not answer."""
        try:
            async with self._session.get(f"{self._base}/panel", auth=self._auth,
                                         timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                self.last_status = await resp.json(content_type=None)
                self._learn_caps(self.last_status)
                self._caps_known = True
                return self.last_status
        except Exception as err:
            _LOG.debug("GET /panel failed: %s", err)
            return None

    # ------------------------------------------------------------------
    # uploads
    # ------------------------------------------------------------------

    def _pick_format(self, photo: bool) -> str:
        """The body format for one upload: "rgb565" (old firmware / QOI rejected),
        "qoi", or "auto" (photo tube: JPEG when it encodes smaller than the QOI)."""
        if Image is None or "qoi" not in self.formats or not self._qoi_ok:
            return "rgb565"
        if photo and "jpeg" in self.formats:
            return "auto"
        return "qoi"

    def _encode_body(self, fmt: str, data: bytes, w: int, h: int) -> tuple[bytes, str]:
        """(body, format param) for one upload attempt. The change-detection hashes
        stay on the raw RGB565 bytes; only the body is encoded."""
        if fmt == "rgb565" or Image is None:
            return data, "rgb565"
        img = rgb565_to_image(data, w, h)   # exact inverse of image_to_rgb565
        if fmt == "qoi":
            return _encode_qoi(img), "qoi"
        # "auto": photo tube - send the JPEG when it is smaller than the QOI
        qoi = _encode_qoi(img)
        jpeg = _encode_jpeg(img)
        return (jpeg, "jpeg") if len(jpeg) < len(qoi) else (qoi, "qoi")

    async def send_tubes(self, tube_bytes: list[bytes], needed_mask: int = 0,
                         photo_tubes=None) -> list[int]:
        """Send the tubes that changed (plus needed ones). Returns sent indices.

        `photo_tubes` (indices) may use JPEG when the clock supports it and the JPEG
        is smaller than the QOI. Tubes whose GIF is playing are skipped unless the
        clock needs them (a /panel upload would stop the GIF).

        When a tube upload fails with a connection error/timeout, the remaining
        tubes of this call are skipped: the clock is unreachable, and retrying
        6 tubes x 6 times would block the app for minutes."""
        await self._ensure_caps()
        todo = self.tubes_to_send(tube_bytes, needed_mask)
        photo = photo_tubes or ()
        todo = [i for i in todo if not (i in self._playing and not (needed_mask & (1 << i)))]
        sent: list[int] = []
        # one request for all of them (HA Tubes 2.5+). Photo tubes may go as JPEG, which the
        # clock only takes in a batch it holds in memory; otherwise they go on their own.
        held = self.batch_buffer > 0
        batched = [i for i in todo if held or i not in photo]
        if self.batch_supported and len(batched) > 1:
            ok, connection_lost = await self._batch_upload(
                [(i, 0, 0, TUBE_W, TUBE_H, tube_bytes[i], i in photo) for i in batched])
            if ok:
                sent.extend(batched)
                todo = [i for i in todo if i not in batched]
            elif connection_lost:
                self.reachable = False
                _LOG.warning("Clock unreachable while sending %d tube(s) - skipping the rest of this send",
                             len(batched))
                return sent
            # any other failure: try the tubes one by one below
        for i in todo:
            ok, connection_lost = await self._send_one(i, tube_bytes[i], photo=i in photo)
            if ok:
                sent.append(i)
            elif connection_lost:
                self.reachable = False
                _LOG.warning("Clock unreachable while sending tube %d - skipping the remaining %d tube(s) of this send",
                             i, len(todo) - len(sent) - 1)
                break
        return sent

    async def _send_one(self, tube: int, data: bytes, photo: bool = False) -> tuple[bool, bool]:
        """Upload one tube. Returns (ok, connection_lost)."""
        ok, connection_lost = await self._upload({"tube": tube}, data, TUBE_W, TUBE_H, photo=photo)
        if ok:
            h = self._hash(data)
            self._last_hash[tube] = h
            self._last_bytes[tube] = data
        return ok, connection_lost

    async def send_tube(self, tube: int, data: bytes, photo: bool = False) -> bool:
        """Upload one whole tube unconditionally, even if its bytes are unchanged
        (used by screen transitions). Returns True on success."""
        await self._ensure_caps()
        ok, connection_lost = await self._upload({"tube": tube}, data, TUBE_W, TUBE_H, photo=photo)
        if connection_lost:
            self.reachable = False
        if ok:
            h = self._hash(data)
            self._last_hash[tube] = h
            self._last_bytes[tube] = data
        return ok

    async def send_rect(self, tube: int, x: int, y: int, w: int, h: int, data: bytes,
                        photo: bool = False) -> bool:
        """Upload a rectangular region of one tube (POST /panel?tube=N&x=X&y=Y&w=W&h=H,
        body of w*h pixels in the clock's upload format). On success the region is
        patched into the tube's last bytes (when known) and its hash recomputed, so
        a later full refresh only resends the tube if the whole picture really
        differs. Returns True on success."""
        if not (0 <= tube < TUBE_COUNT):
            raise ValueError(f"tube must be 0..{TUBE_COUNT - 1}, got {tube}")
        if not (0 <= x and 0 <= y and x + w <= TUBE_W and y + h <= TUBE_H and w > 0 and h > 0):
            raise ValueError(f"rect ({x},{y},{w},{h}) outside the {TUBE_W}x{TUBE_H} tube")
        if len(data) != w * h * 2:
            raise ValueError(f"rect body must be {w * h * 2} bytes (w*h*2), got {len(data)}")
        await self._ensure_caps()
        ok, connection_lost = await self._upload(
            {"tube": tube, "x": x, "y": y, "w": w, "h": h}, data, w, h, photo=photo)
        if connection_lost:
            self.reachable = False
        if not ok:
            return False
        self._record_rect(tube, x, y, w, h, data)
        return True

    def _record_rect(self, tube: int, x: int, y: int, w: int, h: int, data: bytes) -> None:
        """Note that a rectangle of a tube now shows `data` (for change detection)."""
        if x == 0 and y == 0 and w == TUBE_W and h == TUBE_H:
            self._last_bytes[tube] = data
            self._last_hash[tube] = self._hash(data)
            return
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

    async def send_rects(self, rects) -> bool:
        """Upload several rectangles: (tube, x, y, w, h, data[, photo]) each, `data` being
        w*h*2 RGB565 bytes. On HA Tubes 2.5+ they go in POST /panel/batch requests (much
        less per-request overhead), else one send_rect each. Returns True when all of them
        were drawn."""
        items = []
        for r in rects:
            tube, x, y, w, h, data = r[:6]
            if not (0 <= tube < TUBE_COUNT):
                raise ValueError(f"tube must be 0..{TUBE_COUNT - 1}, got {tube}")
            if not (0 <= x and 0 <= y and x + w <= TUBE_W and y + h <= TUBE_H and w > 0 and h > 0):
                raise ValueError(f"rect ({x},{y},{w},{h}) outside the {TUBE_W}x{TUBE_H} tube")
            if len(data) != w * h * 2:
                raise ValueError(f"rect body must be {w * h * 2} bytes (w*h*2), got {len(data)}")
            items.append((tube, x, y, w, h, data, bool(r[6]) if len(r) > 6 else False))
        await self._ensure_caps()
        held = self.batch_buffer > 0
        batched = [i for i in items if held or not i[6]]
        single = items if not (self.batch_supported and len(batched) > 1) else [i for i in items if i not in batched]
        if single is not items:
            ok, connection_lost = await self._batch_upload(batched)
            if connection_lost:
                self.reachable = False
                return False
            if not ok:
                single = items      # any other failure: try them one by one
        for tube, x, y, w, h, data, photo in single:
            if not await self.send_rect(tube, x, y, w, h, data, photo=photo):
                return False
        return True

    async def send_screen(self, tube_bytes: list[bytes], photo_tubes, kind: str,
                          order: list[int], duration_ms: int = 500, gap_ms: int = 90) -> bool | None:
        """Send all six tubes in one batch that the clock draws with a transition of its own
        (HA Tubes 2.5+): kind "wipe" (the whole wipe taking duration_ms), "cascade"
        (gap_ms between tubes) or "flip" (HA Tubes 2.6+: each tube flipping over duration_ms,
        starting gap_ms after the one before it), the tubes in `order`. It replaces whatever the clock is
        still drawing. Returns True when the clock took it, False when it failed, None
        when this clock can't (old firmware, or too big to hold now): send it another way."""
        await self._ensure_caps()
        if not (self.batch_supported and kind in self.transitions and self.batch_buffer > 0):
            return None
        photo = photo_tubes or ()
        fmt = self.upload_format
        rects = [(i, 0, 0, TUBE_W, TUBE_H, tube_bytes[i], i in photo) for i in order]
        body = b"".join(self._batch_record(r, fmt, held=True) for r in rects)
        if len(body) > self.batch_buffer:
            return None
        params = {"transition": kind, "order": ",".join(str(i) for i in order), "interrupt": "1"}
        if kind in ("wipe", "flip"):
            params["duration"] = str(int(duration_ms))
        if kind in ("cascade", "flip"):
            params["gap"] = str(int(gap_ms))
        status = await self._post_batch(rects, body, held=True, params=params)
        if status == "ok":
            return True
        if status == "too_big":
            return None
        if status == "lost":
            self.reachable = False
        return False

    def _batch_cap(self) -> int:
        """Largest body to send in one batch: what the clock can hold in memory, else
        BATCH_MAX_BYTES (a batch it draws as it arrives stalls when bigger)."""
        return self.batch_buffer if self.batch_buffer > 0 else BATCH_MAX_BYTES

    def _batch_record(self, rect, fmt: str, held: bool = False) -> bytes:
        """One POST /panel/batch record: a 14-byte little-endian header (tube, format
        0=rgb565/1=qoi/2=jpeg, x, y, w, h, length) and then the picture. A photo rect may
        go as JPEG, but only in a batch the clock holds in memory (`held`).

        Encoded records are cached (keyed by the rgb565 bytes' sha256, geometry, format
        choice and `held`) in a small LRU, so an unchanged tube is not encoded again;
        reset() clears the cache."""
        tube, x, y, w, h, data = rect[:6]
        photo = len(rect) > 6 and rect[6]
        use = fmt
        if photo and held and fmt == "qoi" and "jpeg" in self.formats:
            use = "auto"
        key = (tube, self._hash(data), x, y, w, h, use, bool(held))
        cached = self._record_cache.get(key)
        if cached is not None:
            self._record_cache.move_to_end(key)
            return cached
        pic, use_fmt = self._encode_body(use, data, w, h)
        code = {"rgb565": 0, "qoi": 1, "jpeg": 2}[use_fmt]
        record = struct.pack("<BBHHHHI", tube, code, x, y, w, h, len(pic)) + pic
        self._record_cache[key] = record
        self._record_cache.move_to_end(key)
        while len(self._record_cache) > BATCH_RECORD_CACHE_MAX:
            self._record_cache.popitem(last=False)
        return record

    def _chunk_ends(self, records: list[bytes], start: int = 0) -> int:
        """Index after the last record that fits one body (see _batch_cap) starting at
        `start` (always at least one record)."""
        cap = self._batch_cap()
        end, size = start, 0
        while end < len(records) and (end == start or size + len(records[end]) <= cap):
            size += len(records[end])
            end += 1
        return end

    def _batch_chunks(self, rects, fmt: str) -> list[tuple[list, bytes]]:
        """The rects encoded and packed into bodies of at most _batch_cap() (a rect bigger
        than that goes in a body of its own). Returns [(rects, body)]."""
        held = self.batch_buffer > 0
        records = [self._batch_record(r, fmt, held) for r in rects]
        chunks, i = [], 0
        while i < len(records):
            j = self._chunk_ends(records, i)
            chunks.append((list(rects[i:j]), b"".join(records[i:j])))
            i = j
        return chunks

    async def _batch_upload(self, rects) -> tuple[bool, bool]:
        """Send (tube, x, y, w, h, data[, photo]) rects with POST /panel/batch, in bodies of
        at most _batch_cap(), each picture encoded once. Returns (ok, connection_lost);
        rects of bodies that went through are recorded even when a later one fails."""
        rects = list(rects)
        fmt, held = self.upload_format, self.batch_buffer > 0
        records = [self._batch_record(r, fmt, held) for r in rects]
        i = 0
        while i < len(rects):
            if (self.upload_format, self.batch_buffer > 0) != (fmt, held):
                # a QOI 400 switched to raw, or the clock can't hold big batches now: re-pack the rest
                fmt, held = self.upload_format, self.batch_buffer > 0
                records[i:] = [self._batch_record(r, fmt, held) for r in rects[i:]]
            j = self._chunk_ends(records, i)
            status = await self._post_batch(rects[i:j], b"".join(records[i:j]), held=held)
            if status == "too_big":
                continue        # batch_buffer is 0 now: smaller bodies
            if status != "ok":
                return False, status == "lost"
            i = j
        return True, False

    async def _post_batch(self, rects, body: bytes, held: bool = False,
                          params: dict | None = None) -> str:
        """One POST /panel/batch, with the same retry / 409 / 503 / QOI-400 handling as
        single uploads. Returns "ok", "fail", "lost" (connection) or "too_big" (413: the
        clock can't hold it now; batch_buffer is then 0 until the capabilities are
        fetched again)."""
        tubes = sorted({r[0] for r in rects})
        fmt = self.upload_format
        async with self._lock:
            for attempt in range(6):
                try:
                    async with self._session.post(
                        f"{self._base}/panel/batch", params=params or None, data=body, auth=self._auth,
                        headers={"Content-Type": "application/octet-stream"},
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as resp:
                        self._sent_log.append((time.monotonic(), len(body)))
                        if resp.status == 200:
                            self._backoff = 1.0
                            self.last_error = None
                            self.reachable = True
                            for r in rects:
                                self._record_rect(*r[:6])
                                self._playing.discard(r[0])  # an upload stops the tube's GIF
                            return "ok"
                        if resp.status == 409:
                            _LOG.warning("Clock returned 409 (not a panel tube) on a batch to tubes %s - "
                                         "re-publishing display=4", tubes)
                            if self._publish_display is not None:
                                try:
                                    await self._publish_display(DISPLAY_PANELS)
                                except Exception as err:
                                    _LOG.warning("Could not publish display=4: %s", err)
                            await self._sleep(1.0)
                            continue
                        if resp.status == 503:
                            wait = _retry_after(resp)
                            if wait is not None:
                                # still drawing the previous batch: try again when it's done
                                await self._sleep(wait)
                                continue
                            _LOG.debug("Clock busy (503) on a batch to tubes %s - retrying", tubes)
                            await self._sleep(self._backoff)
                            self._backoff = min(self._backoff * 2, MAX_BACKOFF)
                            continue
                        if resp.status == 413:
                            _LOG.debug("Clock can't hold a %d-byte batch now - sending smaller ones", len(body))
                            self.batch_buffer = 0
                            self._caps_known = False     # learn the clock's room again next time
                            return "too_big"
                        if resp.status == 400 and fmt == "qoi":
                            if not self._qoi_400_logged:
                                self._qoi_400_logged = True
                                _LOG.warning("Clock answered 400 to a QOI batch (tubes %s) - "
                                             "using raw RGB565 until the next reset()", tubes)
                            self._qoi_ok = False
                            fmt = "rgb565"
                            body = b"".join(self._batch_record(r, fmt, held) for r in rects)
                            continue
                        body_text = await resp.text()
                        self.last_error = f"tubes {tubes}: HTTP {resp.status}: {body_text[:120]}"
                        _LOG.warning("POST /panel/batch tubes %s -> %d %s", tubes, resp.status, body_text[:120])
                        return "fail"
                except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                    self.last_error = f"tubes {tubes}: {err}"
                    _LOG.debug("POST /panel/batch tubes %s failed: %s - retrying", tubes, err)
                    await self._sleep(self._backoff)
                    self._backoff = min(self._backoff * 2, MAX_BACKOFF)
            self.last_error = f"tubes {tubes}: giving up after retries ({self.last_error})"
            _LOG.error("Could not send tubes %s to the clock: %s", tubes, self.last_error)
            return "lost"

    async def _upload(self, params: dict, data: bytes, w: int, h: int,
                      photo: bool = False) -> tuple[bool, bool]:
        """POST /panel with the given query params and the RGB565 body `data` encoded
        per the clock's capabilities, with the shared retry/409/503/400/unreachable
        handling. Returns (ok, connection_lost)."""
        tube = params.get("tube")
        fmt = self._pick_format(photo)
        async with self._lock:
            for attempt in range(6):
                body, use_fmt = self._encode_body(fmt, data, w, h)
                post_params = dict(params)
                if use_fmt != "rgb565":
                    post_params["format"] = use_fmt
                try:
                    async with self._session.post(
                        f"{self._base}/panel", params=post_params,
                        data=body, auth=self._auth,
                        headers={"Content-Type": "application/octet-stream"},
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as resp:
                        self._sent_log.append((time.monotonic(), len(body)))
                        if resp.status == 200:
                            self._backoff = 1.0
                            self.last_error = None
                            self.reachable = True
                            self._playing.discard(tube)   # a /panel upload stops the tube's GIF
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
                            wait = _retry_after(resp)
                            if wait is not None:
                                # the clock is still drawing a batch: try again when it's done
                                await self._sleep(wait)
                                continue
                            if use_fmt == "jpeg":
                                # not enough memory for the JPEG: retry this tube as QOI
                                fmt = "qoi" if ("qoi" in self.formats and self._qoi_ok) else "rgb565"
                                _LOG.debug("Clock busy (503) on a JPEG upload to tube %s - "
                                           "retrying as %s", tube, fmt)
                                continue
                            _LOG.debug("Clock busy (503) on tube %s - retrying", tube)
                            await self._sleep(self._backoff)
                            self._backoff = min(self._backoff * 2, MAX_BACKOFF)
                            continue
                        if resp.status == 400 and use_fmt == "qoi":
                            # this clock does not really take QOI: fall back to raw
                            if not self._qoi_400_logged:
                                self._qoi_400_logged = True
                                _LOG.warning("Clock answered 400 to a QOI upload (tube %s) - "
                                             "using raw RGB565 until the next reset()", tube)
                            self._qoi_ok = False
                            fmt = "rgb565"
                            continue
                        body_text = await resp.text()
                        self.last_error = f"tube {tube}: HTTP {resp.status}: {body_text[:120]}"
                        _LOG.warning("POST /panel tube %s -> %d %s", tube, resp.status, body_text[:120])
                        return False, False
                except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                    self.last_error = f"tube {tube}: {err}"
                    _LOG.debug("POST /panel tube %s failed: %s - retrying", tube, err)
                    await self._sleep(self._backoff)
                    self._backoff = min(self._backoff * 2, MAX_BACKOFF)
            self.last_error = f"tube {tube}: giving up after retries ({self.last_error})"
            _LOG.error("Could not send tube %s to the clock: %s", tube, self.last_error)
            return False, True

    # ------------------------------------------------------------------
    # GIFs (HA Tubes 2.4+): upload / list / play / stop / delete
    # ------------------------------------------------------------------

    async def _gif_request(self, method: str, path: str, params: dict | None = None,
                           data: bytes | None = None, timeout: float = 30):
        """One GIF-endpoint request. Returns (status, body) or None on a connection
        error (which also marks the clock unreachable)."""
        try:
            async with getattr(self._session, method)(
                f"{self._base}{path}", params=params, data=data, auth=self._auth,
                headers={"Content-Type": "application/octet-stream"},
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                body = await resp.read()
                if resp.status == 200:
                    self.reachable = True
                    self.last_error = None
                    self._backoff = 1.0
                return resp.status, body
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            self.last_error = f"{method.upper()} {path}: {err}"
            _LOG.debug("%s %s failed: %s", method.upper(), path, err)
            self.reachable = False
            return None

    async def gif_list(self) -> dict | None:
        """GET /gif -> {"files": [{"name", "size"}], "free": bytes}, or None."""
        try:
            async with self._session.get(f"{self._base}/gif", auth=self._auth,
                                         timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                return await resp.json(content_type=None)
        except Exception as err:
            _LOG.debug("GET /gif failed: %s", err)
            return None

    async def gif_upload(self, name: str, data: bytes) -> bool:
        """POST /gif?name=<name>.gif with the file as body (max 600 KB)."""
        if not GIF_NAME_RE.match(name or ""):
            raise ValueError(f"bad gif name {name!r} (need [A-Za-z0-9._-]{{1,36}}.gif)")
        if len(data) > GIF_MAX_BYTES:
            raise ValueError(f"gif is {len(data)} bytes, the clock takes at most {GIF_MAX_BYTES}")
        async with self._lock:
            for _ in range(3):
                res = await self._gif_request("post", "/gif", {"name": name}, data, timeout=60)
                if res is None:
                    return False
                status, body = res
                if status == 200:
                    self._gif_uploaded[name] = len(data)
                    return True
                if status == 503:
                    await self._sleep(self._backoff)
                    self._backoff = min(self._backoff * 2, MAX_BACKOFF)
                    continue
                self.last_error = f"gif upload {name}: HTTP {status}: {body[:120]!r}"
                _LOG.warning("POST /gif %s -> %d %s", name, status, body[:120])
                return False
            return False

    async def gif_play(self, tube: int, name: str, *, loop: bool = True,
                       bg: str | None = None, x: int | None = None, y: int | None = None) -> bool:
        """POST /gif/play?tube=N&name=<name>[&loop=0|1][&bg=RRGGBB][&x&y].
        503 = two GIFs already play, 409 = not a panel tube, 404 = unknown file."""
        if not GIF_NAME_RE.match(name or ""):
            raise ValueError(f"bad gif name {name!r}")
        params: dict = {"tube": tube, "name": name, "loop": 1 if loop else 0}
        if bg:
            params["bg"] = bg
        if x is not None:
            params["x"] = x
        if y is not None:
            params["y"] = y
        async with self._lock:
            res = await self._gif_request("post", "/gif/play", params)
        if res is None:
            return False
        status, body = res
        if status == 200:
            self._playing.add(tube)
            return True
        self.last_error = f"gif play {name} on tube {tube}: HTTP {status}: {body[:120]!r}"
        _LOG.warning("POST /gif/play tube %s %s -> %d %s", tube, name, status, body[:120])
        return False

    async def gif_stop(self, tube: int | None = None) -> bool:
        """POST /gif/stop[?tube=N] - stop one tube's GIF, or all of them."""
        params = {"tube": tube} if tube is not None else None
        async with self._lock:
            res = await self._gif_request("post", "/gif/stop", params)
        if res is None:
            return False
        status, _body = res
        if status == 200:
            if tube is None:
                self._playing.clear()
            else:
                self._playing.discard(tube)
            return True
        return False

    async def gif_delete(self, name: str) -> bool:
        """DELETE /gif?name=<name> - remove an uploaded GIF from the clock."""
        async with self._lock:
            res = await self._gif_request("delete", "/gif", {"name": name})
        if res is None:
            return False
        status, _body = res
        if status == 200:
            self._gif_uploaded.pop(name, None)
            return True
        return False
