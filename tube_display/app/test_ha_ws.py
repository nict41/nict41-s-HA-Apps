"""TemplateCache (ha_ws.py) tests against a fake Home Assistant websocket server.

The fake server (aiohttp.web WebSocketResponse on a TestServer) speaks the HA
protocol: auth_required -> auth -> auth_ok/auth_invalid, render_template
subscriptions with result + event messages, unsubscribe_events. No network
beyond the local test server. Async tests follow test_api.py: asyncio.run.
"""

import asyncio
import json
import time

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from ha_ws import WRAP_END, WRAP_START, TemplateCache, TemplateError


class Err:
    """Marks a template whose event is an error, not a result."""

    def __init__(self, message):
        self.message = message


class FakeHA:
    """A minimal HA websocket endpoint. `renders` maps an unwrapped template to
    the value HA would produce for it (str, native JSON value, or Err)."""

    def __init__(self, token="tok", auth_ok=True):
        self.token = token
        self.auth_ok = auth_ok
        self.renders = {}
        self.silent = False          # accept subscriptions but never send an event
        self.fail_subs = set()       # templates whose subscribe result is success=false
        self.drop_once = False       # close the first connection right after auth_ok
        self.received = []           # every message the client sent
        self.unsubscribed = []
        self.connections = 0
        self.subs = {}               # sub id -> wrapped template
        self.ws = None
        app = web.Application()
        app.router.add_get("/websocket", self.handler)
        self.server = TestServer(app)

    @property
    def url(self):
        return str(self.server.make_url("/websocket"))

    def _wrap(self, value):
        if isinstance(value, str):
            return WRAP_START + value + WRAP_END
        return value  # native JSON type, like an unwrapped template would give

    async def handler(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.ws = ws
        self.connections += 1
        await ws.send_json({"type": "auth_required"})
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                break
            data = json.loads(msg.data)
            self.received.append(data)
            mtype = data.get("type")
            if mtype == "auth":
                if self.auth_ok and data.get("access_token") == self.token:
                    await ws.send_json({"type": "auth_ok"})
                    if self.drop_once:
                        self.drop_once = False
                        await ws.close()
                        break
                else:
                    await ws.send_json({"type": "auth_invalid", "message": "Invalid access token"})
            elif mtype == "render_template":
                tid = data["id"]
                template = data["template"]
                self.subs[tid] = template
                inner = template[len(WRAP_START):-len(WRAP_END)]
                if inner in self.fail_subs:
                    await ws.send_json({"id": tid, "type": "result", "success": False,
                                        "error": {"code": "unknown_error", "message": "nope"}})
                    continue
                await ws.send_json({"id": tid, "type": "result", "success": True, "result": None})
                if not self.silent:
                    await self._event(tid, self.renders.get(inner, "42"))
            elif mtype == "unsubscribe_events":
                self.unsubscribed.append(data["subscription"])
        return ws

    async def _event(self, tid, value):
        if isinstance(value, Err):
            await self.ws.send_json({"id": tid, "type": "event",
                                     "event": {"error": value.message, "level": "ERROR"}})
        else:
            await self.ws.send_json({"id": tid, "type": "event",
                                     "event": {"result": self._wrap(value), "listeners": {}}})

    async def push(self, template, value):
        """Send an event for every subscription of `template` (unwrapped)."""
        wrapped = WRAP_START + template + WRAP_END
        for tid, t in list(self.subs.items()):
            if t == wrapped:
                await self._event(tid, value)

    async def send_raw(self, msg):
        await self.ws.send_json(msg)

    def sub_count(self, template):
        wrapped = WRAP_START + template + WRAP_END
        return sum(1 for t in self.subs.values() if t == wrapped)

    def sent_templates(self):
        return [m["template"] for m in self.received if m.get("type") == "render_template"]


async def wait_for(cond, timeout=5.0, what="condition"):
    t0 = time.monotonic()
    while not cond():
        if time.monotonic() - t0 > timeout:
            raise AssertionError(f"timeout waiting for {what}")
        await asyncio.sleep(0.01)


def _run(coro):
    return asyncio.run(coro)


class _Env:
    """A started fake server + client session + cache."""

    def __init__(self, ha, session, cache):
        self.ha = ha
        self.session = session
        self.cache = cache

    async def close(self):
        await self.cache.stop()
        await self.session.close()
        await self.ha.server.close()


async def _make(cache_kwargs=None, ha_kwargs=None, wait_connected=True):
    ha = FakeHA(**(ha_kwargs or {}))
    await ha.server.start_server()
    session = aiohttp.ClientSession()
    cache = TemplateCache(session, ha.token, url=ha.url, **(cache_kwargs or {}))
    await cache.start()
    env = _Env(ha, session, cache)
    try:
        if wait_connected:
            await wait_for(lambda: cache.connected, what="connect")
    except Exception:
        await env.close()
        raise
    return env


# ----------------------------------------------------------------------
# auth / protocol
# ----------------------------------------------------------------------

def test_auth_ok_and_wrapped_template_sent():
    async def check():
        env = await _make()
        try:
            env.ha.renders["{{ 1 + 1 }}"] = "2"
            assert await env.cache.render("{{ 1 + 1 }}") == "2"
            # the template went out wrapped in the markers
            assert env.ha.sent_templates() == [WRAP_START + "{{ 1 + 1 }}" + WRAP_END]
            assert env.ha.received[0] == {"type": "auth", "access_token": "tok"}
            assert env.cache.stats() == {"connected": True, "subscriptions": 1,
                                         "fallback_renders": 0}
        finally:
            await env.close()
    _run(check())


def test_auth_invalid_does_not_crash_and_falls_back():
    async def check():
        rest_calls = []

        async def rest(template):
            rest_calls.append(template)
            return "rest:" + template

        env = await _make({"rest_renderer": rest}, {"auth_ok": False}, wait_connected=False)
        try:
            await asyncio.sleep(0.3)  # let it try (and fail) a few times
            assert not env.cache.connected
            assert env.ha.connections >= 1
            assert await env.cache.render("{{ x }}") == "rest:{{ x }}"
            assert env.cache.stats()["fallback_renders"] == 1
        finally:
            await env.close()
    _run(check())


def test_subscribe_failure_raises_template_error():
    async def check():
        env = await _make()
        try:
            env.ha.fail_subs.add("{{ bad }}")
            try:
                await env.cache.render("{{ bad }}")
                raise AssertionError("expected TemplateError")
            except TemplateError as err:
                assert "nope" in str(err)
        finally:
            await env.close()
    _run(check())


# ----------------------------------------------------------------------
# value handling
# ----------------------------------------------------------------------

def test_zero_padded_and_whitespace_preserved():
    async def check():
        env = await _make()
        try:
            env.ha.renders["{{ x }}"] = "07"
            assert await env.cache.render("{{ x }}") == "07"
            await env.ha.push("{{ x }}", "  padded  ")
            await wait_for(lambda: env.cache._subs["{{ x }}"].value == "  padded  ",
                           what="push")
            assert await env.cache.render("{{ x }}") == "  padded  "
        finally:
            await env.close()
    _run(check())


def test_list_result_arrives_as_exact_inner_string():
    async def check():
        env = await _make()
        try:
            # wrapped: HA returns the string "<<TD<[1, 2]>TD>>"
            env.ha.renders["{{ l }}"] = "[1, 2]"
            assert await env.cache.render("{{ l }}") == "[1, 2]"
            # unwrapped/native: HA parses it into a JSON list, we json.dumps it back
            env.ha.renders["{{ n }}"] = [1, 2]
            assert await env.cache.render("{{ n }}") == json.dumps([1, 2])
        finally:
            await env.close()
    _run(check())


def test_error_event_raises_template_error():
    async def check():
        env = await _make()
        try:
            env.ha.renders["{{ boom }}"] = Err("template exploded")
            try:
                await env.cache.render("{{ boom }}")
                raise AssertionError("expected TemplateError")
            except TemplateError as err:
                assert "template exploded" in str(err)
            # a later good event clears the error
            await env.ha.push("{{ boom }}", "ok")
            await wait_for(lambda: env.cache._subs["{{ boom }}"].error is None, what="recover")
            assert await env.cache.render("{{ boom }}") == "ok"
        finally:
            await env.close()
    _run(check())


# ----------------------------------------------------------------------
# subscriptions
# ----------------------------------------------------------------------

def test_concurrent_renders_share_one_subscription():
    async def check():
        env = await _make()
        try:
            env.ha.renders["{{ t }}"] = "v"
            results = await asyncio.gather(env.cache.render("{{ t }}"),
                                           env.cache.render("{{ t }}"),
                                           env.cache.render("{{ t }}"))
            assert results == ["v", "v", "v"]
            assert env.ha.sub_count("{{ t }}") == 1
            assert env.cache.stats()["subscriptions"] == 1
        finally:
            await env.close()
    _run(check())


def test_on_change_fires_on_change_only():
    async def check():
        changes = []
        env = await _make({"on_change": changes.append})
        try:
            env.ha.renders["{{ t }}"] = "a"
            assert await env.cache.render("{{ t }}") == "a"
            assert changes == []                      # first event: no callback
            await env.ha.push("{{ t }}", "a")
            await asyncio.sleep(0.1)
            assert changes == []                      # identical value: no callback
            await env.ha.push("{{ t }}", "b")
            await wait_for(lambda: changes == ["{{ t }}"], what="change")
            await env.ha.push("{{ t }}", "b")
            await asyncio.sleep(0.1)
            assert changes == ["{{ t }}"]             # identical again: nothing
            await env.ha.push("{{ t }}", Err("bad"))
            await wait_for(lambda: len(changes) == 2, what="error change")
            await env.ha.push("{{ t }}", Err("bad"))
            await asyncio.sleep(0.1)
            assert len(changes) == 2                  # same error: nothing
        finally:
            await env.close()
    _run(check())


def test_bad_on_change_callback_cannot_kill_reader():
    async def check():
        def bad(template):
            raise RuntimeError("callback bug")

        env = await _make({"on_change": bad})
        try:
            env.ha.renders["{{ t }}"] = "a"
            assert await env.cache.render("{{ t }}") == "a"
            await env.ha.push("{{ t }}", "b")   # callback raises; reader must survive
            await wait_for(lambda: env.cache._subs["{{ t }}"].value == "b", what="push")
            assert await env.cache.render("{{ t }}") == "b"
        finally:
            await env.close()
    _run(check())


# ----------------------------------------------------------------------
# fallback / reconnect / housekeeping
# ----------------------------------------------------------------------

def test_fallback_when_server_down():
    async def check():
        session = aiohttp.ClientSession()
        calls = []

        async def rest(template):
            calls.append(template)
            return "rest"

        cache = TemplateCache(session, "tok", url="http://127.0.0.1:1/websocket",
                              rest_renderer=rest)
        await cache.start()
        try:
            await asyncio.sleep(0.1)
            assert not cache.connected
            assert await cache.render("{{ x }}") == "rest"
            assert calls == ["{{ x }}"]
            assert cache.stats()["fallback_renders"] == 1
        finally:
            await cache.stop()
            await session.close()
    _run(check())


def test_no_rest_renderer_and_disconnected_raises():
    async def check():
        session = aiohttp.ClientSession()
        cache = TemplateCache(session, "tok", url="http://127.0.0.1:1/websocket")
        await cache.start()
        try:
            await asyncio.sleep(0.1)
            try:
                await cache.render("{{ x }}")
                raise AssertionError("expected TemplateError")
            except TemplateError as err:
                assert "not connected" in str(err)
        finally:
            await cache.stop()
            await session.close()
    _run(check())


def test_first_result_timeout_uses_rest_but_keeps_subscription():
    async def check():
        async def rest(template):
            return "rest"

        env = await _make({"rest_renderer": rest, "first_result_timeout": 0.2})
        try:
            env.ha.silent = True
            assert await env.cache.render("{{ t }}") == "rest"
            assert env.cache.stats()["fallback_renders"] == 1
            assert env.cache.stats()["subscriptions"] == 1   # subscription kept
            env.ha.silent = False
            await env.ha.push("{{ t }}", "ws")
            await wait_for(lambda: env.cache._subs["{{ t }}"].value == "ws", what="event")
            assert await env.cache.render("{{ t }}") == "ws"
            assert env.cache.stats()["fallback_renders"] == 1
        finally:
            await env.close()
    _run(check())


def test_stale_cache_served_while_disconnected_and_resubscribe_after_drop():
    async def check():
        env = await _make()
        try:
            env.ha.renders["{{ t }}"] = "v1"
            assert await env.cache.render("{{ t }}") == "v1"
            # server drops the connection
            env.ha.drop_once = True
            await env.ha.ws.close()
            await wait_for(lambda: not env.cache.connected, what="disconnect")
            # stale is better than blank: the cached value is still served
            assert await env.cache.render("{{ t }}") == "v1"
            # it reconnects (backoff 1 s) and re-subscribes with a new id
            await wait_for(lambda: env.cache.connected, timeout=10, what="reconnect")
            await wait_for(lambda: env.ha.sub_count("{{ t }}") >= 2, what="re-subscribe")
            assert await env.cache.render("{{ t }}") == "v1"
            assert env.ha.connections >= 2
        finally:
            await env.close()
    _run(check())


def test_idle_unsubscribe_with_fake_clock():
    async def check():
        clock = [1000.0]

        def now():
            return clock[0]

        env = await _make({"now": now, "idle_ttl": 10.0})
        try:
            env.ha.renders["{{ t }}"] = "v"
            assert await env.cache.render("{{ t }}") == "v"
            assert env.cache.stats()["subscriptions"] == 1
            # jump past the housekeeping interval (60 s) and the idle ttl
            clock[0] += 120.0
            # any message makes the reader run its housekeeping pass
            await env.ha.send_raw({"id": 424242, "type": "result", "success": True})
            await wait_for(lambda: env.cache.stats()["subscriptions"] == 0, what="unsubscribe")
            assert env.ha.unsubscribed, "expected an unsubscribe_events message"
            # the template is forgotten: a new render subscribes again
            assert await env.cache.render("{{ t }}") == "v"
            assert env.cache.stats()["subscriptions"] == 1
        finally:
            await env.close()
    _run(check())
