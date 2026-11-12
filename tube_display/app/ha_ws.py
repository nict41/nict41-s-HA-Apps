"""Live template rendering over Home Assistant's websocket API.

Instead of POSTing every template to /api/template on each refresh, this
subscribes once per template with a `render_template` subscription and keeps
the latest result, so a screen updates the moment the entity behind it
changes.

HA parses template results into native JSON types over the websocket (a
template rendering "[1, 2]" arrives as a list), while the REST API returns the
raw string. To get exactly the REST string every template is wrapped in
WRAP_START/WRAP_END markers before it is sent, so the result comes back as one
string and the markers are stripped again.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Awaitable, Callable

import aiohttp

_LOG = logging.getLogger(__name__)

DEFAULT_WS_URL = "ws://supervisor/core/websocket"

# Markers that make HA return the rendered template as one string instead of a
# parsed JSON value; stripped again on arrival.
WRAP_START = "<<TD<"
WRAP_END = ">TD>>"

_MAX_BACKOFF = 60.0
_HOUSEKEEP_EVERY = 60.0


class TemplateError(Exception):
    pass


class _Sub:
    """One subscribed template: its latest result/error and bookkeeping."""

    __slots__ = ("template", "sub_id", "value", "error", "has_result", "last_used", "pending")

    def __init__(self, template: str, sub_id: int, now: float) -> None:
        self.template = template
        self.sub_id = sub_id
        self.value: str | None = None
        self.error: str | None = None
        self.has_result = False
        self.last_used = now
        # Set while a render() call waits for the first event of a fresh subscription.
        self.pending: asyncio.Future | None = None


class TemplateCache:
    """Keeps the latest websocket-rendered value of every template used.

    `rest_renderer` is the fallback used while the websocket is down (or for a
    first render that doesn't arrive in time); `on_change` is called whenever a
    subscribed template's text or error changes.
    """

    def __init__(self, session: aiohttp.ClientSession, token: str,
                 url: str = DEFAULT_WS_URL,
                 rest_renderer: Callable[[str], Awaitable[str]] | None = None,
                 on_change: Callable[[str], None] | None = None,
                 now: Callable[[], float] = time.monotonic,
                 first_result_timeout: float = 5.0,
                 idle_ttl: float = 1800.0,
                 max_subscriptions: int = 500) -> None:
        self._session = session
        self._token = token
        # aiohttp's ws_connect takes http(s) URLs; ws:// is normalized the same way
        self._url = url.replace("ws://", "http://", 1).replace("wss://", "https://", 1)
        self._rest = rest_renderer
        self._on_change = on_change
        self._now = now
        self._first_timeout = first_result_timeout
        self._idle_ttl = idle_ttl
        self._max_subs = max_subscriptions
        self._subs: dict[str, _Sub] = {}
        self._task: asyncio.Task | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._connected = False
        self._next_id = 1
        self._fallback_renders = 0
        self._stop = False

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the background reader task (connect, auth, subscribe, read)."""
        if self._task is None or self._task.done():
            self._stop = False
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Cancel the reader task and close the websocket."""
        self._stop = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await self._close_ws()

    @property
    def connected(self) -> bool:
        return self._connected

    def stats(self) -> dict:
        return {"connected": self._connected, "subscriptions": len(self._subs),
                "fallback_renders": self._fallback_renders}

    async def _close_ws(self) -> None:
        ws, self._ws = self._ws, None
        self._connected = False
        if ws is not None and not ws.closed:
            try:
                await ws.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # render
    # ------------------------------------------------------------------

    async def render(self, template: str) -> str:
        """The latest rendered text of `template` (cached if we have one)."""
        sub = self._subs.get(template)
        if sub is not None:
            sub.last_used = self._now()
            if sub.has_result:
                if sub.error is not None:
                    raise TemplateError(sub.error)
                return sub.value  # type: ignore[return-value]
        if not self._connected or (sub is None and len(self._subs) >= self._max_subs):
            return await self._fallback(template)
        if sub is None:
            try:
                sub = await self._subscribe(template)
            except Exception:
                return await self._fallback(template)
        # Wait for the first event of a fresh subscription; concurrent render()
        # calls for the same template share the one pending future.
        if not sub.has_result:
            if sub.pending is None:
                sub.pending = asyncio.get_running_loop().create_future()
            fut = sub.pending
            try:
                await asyncio.wait_for(asyncio.shield(fut), timeout=self._first_timeout)
            except asyncio.TimeoutError:
                # Keep the subscription (it may still deliver later) but answer
                # this call from REST so the screen isn't blank.
                return await self._fallback(template)
            finally:
                if sub.pending is fut:
                    sub.pending = None
        if sub.error is not None:
            raise TemplateError(sub.error)
        if sub.has_result:
            return sub.value  # type: ignore[return-value]
        return await self._fallback(template)

    async def _fallback(self, template: str) -> str:
        if self._rest is None:
            raise TemplateError("not connected")
        self._fallback_renders += 1
        try:
            return await self._rest(template)
        except Exception as err:
            raise TemplateError(str(err)) from err

    async def _subscribe(self, template: str) -> _Sub:
        """Send the render_template subscription; concurrent callers share it."""
        sub = self._subs.get(template)
        if sub is not None:
            return sub
        sub = _Sub(template, self._next_id, self._now())
        self._next_id += 1
        self._subs[template] = sub
        if self._ws is not None and not self._ws.closed:
            await self._send({"id": sub.sub_id, "type": "render_template",
                              "template": WRAP_START + template + WRAP_END,
                              "report_errors": True})
        return sub

    async def _send(self, msg: dict) -> None:
        ws = self._ws
        if ws is None or ws.closed:
            raise TemplateError("not connected")
        await ws.send_json(msg)

    # ------------------------------------------------------------------
    # reader loop
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stop:
            try:
                authed = await self._connect_and_read()
                if authed:
                    backoff = 1.0  # a good connection resets the backoff
            except asyncio.CancelledError:
                raise
            except Exception as err:
                _LOG.warning("TemplateCache: websocket error: %s", err)
            await self._close_ws()
            if self._stop:
                return
            _LOG.info("TemplateCache: reconnecting in %.0f s", backoff)
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                raise
            backoff = min(backoff * 2, _MAX_BACKOFF)

    async def _connect_and_read(self) -> bool:
        """One connection session; returns True if authentication succeeded."""
        authed = False
        async with self._session.ws_connect(self._url) as ws:
            self._ws = ws
            # HA opens with auth_required; answering that (below) is the only auth sent, since a
            # second auth message after auth_ok would be an invalid command
            last_housekeep = self._now()
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE,
                                    aiohttp.WSMsgType.ERROR):
                        break
                    continue
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    continue
                mtype = data.get("type")
                if mtype == "auth_required":
                    await ws.send_json({"type": "auth", "access_token": self._token})
                elif mtype == "auth_ok":
                    authed = True
                    self._connected = True
                    _LOG.info("TemplateCache: connected, re-subscribing %d templates",
                              len(self._subs))
                    await self._resubscribe_all()
                elif mtype == "auth_invalid":
                    # Bad token: retry with backoff, never crash the app.
                    _LOG.error("TemplateCache: auth_invalid: %s", data.get("message"))
                    raise TemplateError(f"auth_invalid: {data.get('message')}")
                elif mtype == "result":
                    self._handle_result(data)
                elif mtype == "event":
                    self._handle_event(data)
                # housekeeping: drop idle subscriptions about once a minute
                now = self._now()
                if now - last_housekeep >= _HOUSEKEEP_EVERY:
                    last_housekeep = now
                    await self._housekeep(now)
            # loop ended: server closed the connection
        return authed

    async def _resubscribe_all(self) -> None:
        for template, sub in list(self._subs.items()):
            sub.sub_id = self._next_id
            self._next_id += 1
            sub.pending = None
            try:
                await self._send({"id": sub.sub_id, "type": "render_template",
                                  "template": WRAP_START + template + WRAP_END,
                                  "report_errors": True})
            except Exception as err:
                _LOG.warning("TemplateCache: re-subscribe failed for %r: %s", template, err)

    def _handle_result(self, data: dict) -> None:
        sub = self._sub_by_id(data.get("id"))
        if sub is None:
            return
        if not data.get("success"):
            err = data.get("error") or {}
            msg = err.get("message") if isinstance(err, dict) else str(err)
            self._set_error(sub, f"subscribe failed: {msg}")
        # success: the first event follows; nothing to do here

    def _handle_event(self, data: dict) -> None:
        sub = self._sub_by_id(data.get("id"))
        if sub is None:
            return
        event = data.get("event") or {}
        if "error" in event:
            self._set_error(sub, str(event.get("error")))
        elif "result" in event:
            self._set_value(sub, _unwrap(event["result"]))

    def _sub_by_id(self, sub_id: object) -> _Sub | None:
        for sub in self._subs.values():
            if sub.sub_id == sub_id:
                return sub
        return None

    def _set_value(self, sub: _Sub, text: str) -> None:
        changed = sub.has_result and (sub.value != text or sub.error is not None)
        sub.value = text
        sub.error = None
        sub.has_result = True
        sub.last_used = self._now()
        if sub.pending is not None and not sub.pending.done():
            sub.pending.set_result(None)
        if changed:
            self._notify(sub.template)

    def _set_error(self, sub: _Sub, message: str) -> None:
        changed = sub.has_result and (sub.error != message or sub.value is not None)
        sub.error = message
        sub.value = None
        sub.has_result = True
        sub.last_used = self._now()
        if sub.pending is not None and not sub.pending.done():
            sub.pending.set_result(None)
        if changed:
            self._notify(sub.template)

    def _notify(self, template: str) -> None:
        if self._on_change is None:
            return
        try:
            self._on_change(template)
        except Exception:
            _LOG.exception("TemplateCache: on_change callback failed")

    async def _housekeep(self, now: float) -> None:
        """Unsubscribe templates not used for idle_ttl seconds and forget them."""
        stale = [t for t, s in self._subs.items() if now - s.last_used > self._idle_ttl]
        for template in stale:
            sub = self._subs.pop(template)
            if self._ws is not None and not self._ws.closed:
                try:
                    await self._send({"id": self._next_id, "type": "unsubscribe_events",
                                      "subscription": sub.sub_id})
                    self._next_id += 1
                except Exception as err:
                    _LOG.debug("TemplateCache: unsubscribe failed for %r: %s", template, err)
            _LOG.debug("TemplateCache: dropped idle subscription %r", template)


def _unwrap(result: object) -> str:
    """Turn a websocket result into the exact string REST would have returned."""
    if isinstance(result, str):
        if result.startswith(WRAP_START) and result.endswith(WRAP_END):
            # inner text unchanged - no strip(), "07" must stay "07"
            return result[len(WRAP_START):-len(WRAP_END)]
        return result
    if isinstance(result, (list, dict)):
        return json.dumps(result)
    return str(result)
