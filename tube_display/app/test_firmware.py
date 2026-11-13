"""FirmwarePublisher (firmware.py) tests against a fake GitHub API + download
server (both aiohttp TestServers, no real network). Async tests follow
test_api.py: asyncio.run.
"""

import asyncio
import hashlib
import json
import os

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from firmware import FirmwarePublisher, host_base_url

FIRMWARE_BYTES = b"\x00\x01\x02FAKE-FIRMWARE" * 100


class FakeGitHub:
    """Serves /repos/<repo>/releases/latest (with ETag/304) and the .bin files."""

    def __init__(self, version="2.3.0", with_asset=True, bin_bytes=FIRMWARE_BYTES,
                 etag='"v1"', fail_api=False):
        self.version = version
        self.with_asset = with_asset
        self.bin_bytes = bin_bytes
        self.etag = etag
        self.fail_api = fail_api
        self.api_hits = 0
        self.download_hits = 0
        self.last_headers = {}
        app = web.Application()
        app.router.add_get("/repos/{repo:.*}/releases/latest", self.latest)
        app.router.add_get("/dl/firmware.bin", self.download)
        self.server = TestServer(app)

    @property
    def api_base(self):
        return str(self.server.make_url("")).rstrip("/")

    @property
    def download_url(self):
        return str(self.server.make_url("/dl/firmware.bin"))

    async def latest(self, request):
        self.api_hits += 1
        self.last_headers = dict(request.headers)
        if self.fail_api:
            return web.json_response({"message": "boom"}, status=500)
        if request.headers.get("If-None-Match") == self.etag:
            return web.Response(status=304)
        name = f"firmware-ipstube-{self.version}.bin"
        assets = []
        if self.with_asset:
            assets.append({"name": name, "browser_download_url": self.download_url})
        return web.json_response({
            "tag_name": f"v{self.version}",
            "html_url": f"https://github.com/nict41/HA-Tubes/releases/tag/v{self.version}",
            "assets": assets,
        }, headers={"ETag": self.etag})

    async def download(self, request):
        self.download_hits += 1
        return web.Response(body=self.bin_bytes)


class FakeSupervisor:
    """Serves /network/info."""

    def __init__(self, payload=None, status=200, network=None, self_status=200):
        self.payload = payload if payload is not None else {
            "result": "ok",
            "data": {"interfaces": [
                {"interface": "enp1s0", "primary": True,
                 "ipv4": {"address": ["192.168.8.25/24"]}},
                {"interface": "eth0", "primary": False,
                 "ipv4": {"address": ["10.0.0.5/24"]}},
            ]},
        }
        self.status = status
        self.network = network if network is not None else {"8098/tcp": 8098}
        self.self_status = self_status
        self.seen_auth = None
        app = web.Application()
        app.router.add_get("/network/info", self.info)
        app.router.add_get("/addons/self/info", self.self_info)
        self.server = TestServer(app)

    async def info(self, request):
        self.seen_auth = request.headers.get("Authorization")
        if self.status != 200:
            return web.json_response({"message": "nope"}, status=self.status)
        return web.json_response(self.payload)

    async def self_info(self, request):
        if self.self_status != 200:
            return web.json_response({"message": "nope"}, status=self.self_status)
        return web.json_response({"result": "ok", "data": {"network": self.network}})


def _run(coro):
    return asyncio.run(coro)


class Recorder:
    def __init__(self, clock_ids=("clock1", "clock2")):
        self.published = []          # (topic, payload)
        self.clock_ids = list(clock_ids)

    async def publish(self, topic, payload):
        self.published.append((topic, payload))

    def clock_ids_fn(self):
        return list(self.clock_ids)


async def _make_publisher(tmp_path, gh, base="http://192.168.8.25:8098", **kwargs):
    session = aiohttp.ClientSession()
    rec = Recorder()

    async def base_url():
        return base

    kwargs.setdefault("variant", "ipstube")   # the release files below are named for it
    pub = FirmwarePublisher(session, str(tmp_path), rec.publish, rec.clock_ids_fn,
                            base_url, api_base=gh.api_base, **kwargs)
    return session, rec, pub


# ----------------------------------------------------------------------
# check_now
# ----------------------------------------------------------------------

def test_publishes_correct_payload_to_every_clock(tmp_path):
    async def check():
        gh = FakeGitHub()
        await gh.server.start_server()
        session, rec, pub = await _make_publisher(tmp_path, gh)
        try:
            payload = await pub.check_now()
            assert payload is not None
            assert payload["version"] == "2.3.0"
            assert payload["url"] == "http://192.168.8.25:8098/firmware/firmware-ipstube-2.3.0.bin"
            assert payload["md5"] == hashlib.md5(FIRMWARE_BYTES).hexdigest()
            assert payload["release_url"] == \
                "https://github.com/nict41/HA-Tubes/releases/tag/v2.3.0"
            assert payload["title"] == "HA Tubes 2.3.0"
            assert pub.latest == payload
            assert pub.last_error is None
            # one retained message per clock, on the right topic
            topics = sorted(t for t, _ in rec.published)
            assert topics == ["clock/clock1/ota/latest", "clock/clock2/ota/latest"]
            for _, text in rec.published:
                assert json.loads(text) == payload
            # the file and its md5 sidecar are in the data dir
            name = "firmware-ipstube-2.3.0.bin"
            assert open(os.path.join(str(tmp_path), name), "rb").read() == FIRMWARE_BYTES
            assert open(os.path.join(str(tmp_path), name + ".md5")).read() == payload["md5"]
            # GitHub was asked with the right headers, no personal data
            assert gh.last_headers.get("User-Agent") == "tube-display"
            assert gh.last_headers.get("Accept") == "application/vnd.github+json"
            assert "Authorization" not in gh.last_headers
        finally:
            await session.close()
            await gh.server.close()
    _run(check())


def test_304_republishes_without_redownloading(tmp_path):
    async def check():
        gh = FakeGitHub()
        await gh.server.start_server()
        session, rec, pub = await _make_publisher(tmp_path, gh)
        try:
            first = await pub.check_now()
            assert first is not None
            assert gh.download_hits == 1
            n_published = len(rec.published)
            second = await pub.check_now()      # If-None-Match matches -> 304
            assert second == first
            assert gh.api_hits == 2
            assert gh.download_hits == 1        # no re-download
            assert len(rec.published) == n_published + 2   # republished to both clocks
        finally:
            await session.close()
            await gh.server.close()
    _run(check())


def test_missing_asset_returns_none(tmp_path):
    async def check():
        gh = FakeGitHub(with_asset=False)
        await gh.server.start_server()
        session, rec, pub = await _make_publisher(tmp_path, gh)
        try:
            assert await pub.check_now() is None
            assert "no firmware-ipstube-2.3.0.bin asset" in pub.last_error
            assert rec.published == []
        finally:
            await session.close()
            await gh.server.close()
    _run(check())


def test_oversize_download_aborted(tmp_path):
    async def check():
        gh = FakeGitHub(bin_bytes=b"x" * (4 * 1024 * 1024 + 10))
        await gh.server.start_server()
        session, rec, pub = await _make_publisher(tmp_path, gh)
        try:
            assert await pub.check_now() is None
            assert "larger than" in pub.last_error
            # no .bin and no leftover .part
            assert os.listdir(str(tmp_path)) == []
            assert rec.published == []
        finally:
            await session.close()
            await gh.server.close()
    _run(check())


def test_pruning_keeps_newest_two_bins(tmp_path):
    async def check():
        gh = FakeGitHub()
        await gh.server.start_server()
        session, rec, pub = await _make_publisher(tmp_path, gh)
        try:
            # three pre-existing files, oldest first by mtime
            for i, name in enumerate(["firmware-ipstube-2.0.0.bin",
                                      "firmware-ipstube-2.1.0.bin",
                                      "firmware-ipstube-2.2.0.bin"]):
                p = tmp_path / name
                p.write_bytes(b"old" + bytes([i]))
                os.utime(p, (1000 + i, 1000 + i))
                (tmp_path / (name + ".md5")).write_text("0" * 32)
            await pub.check_now()   # downloads 2.3.0, then prunes to the newest 2
            left = sorted(n for n in os.listdir(str(tmp_path)) if n.endswith(".bin"))
            assert left == ["firmware-ipstube-2.2.0.bin", "firmware-ipstube-2.3.0.bin"]
            assert not os.path.exists(str(tmp_path / "firmware-ipstube-2.0.0.bin.md5"))
            assert os.path.exists(str(tmp_path / "firmware-ipstube-2.2.0.bin.md5"))
        finally:
            await session.close()
            await gh.server.close()
    _run(check())


def test_file_path_security(tmp_path):
    async def check():
        gh = FakeGitHub()
        await gh.server.start_server()
        session, rec, pub = await _make_publisher(tmp_path, gh)
        try:
            await pub.check_now()
            good = pub.file_path("firmware-ipstube-2.3.0.bin")
            assert good == os.path.abspath(str(tmp_path / "firmware-ipstube-2.3.0.bin"))
            assert pub.file_path("../screens.yaml") is None
            assert pub.file_path("..%2Fx") is None
            assert pub.file_path("firmware-ipstube-9.9.9.bin") is None   # not on disk
            assert pub.file_path("firmware-x.bin.md5") is None
            assert pub.file_path("") is None
            assert pub.file_path("sub/firmware-x.bin") is None
        finally:
            await session.close()
            await gh.server.close()
    _run(check())


def test_url_too_long_or_https_base_rejected(tmp_path):
    async def check():
        gh = FakeGitHub()
        await gh.server.start_server()
        # https base -> the clock can't use it
        session, rec, pub = await _make_publisher(tmp_path, gh, base="https://example.com")
        try:
            assert await pub.check_now() is None
            assert "not usable" in pub.last_error
            assert rec.published == []
        finally:
            await session.close()
        # base so long the url exceeds 127 chars
        session, rec, pub = await _make_publisher(
            tmp_path, gh, base="http://" + "a" * 130 + ":8098")
        try:
            assert await pub.check_now() is None
            assert "not usable" in pub.last_error
            assert rec.published == []
        finally:
            await session.close()
        # unknown address
        session, rec, pub = await _make_publisher(tmp_path, gh, base=None)
        try:
            assert await pub.check_now() is None
            assert pub.last_error == "app address unknown"
            assert rec.published == []
        finally:
            await session.close()
            await gh.server.close()
    _run(check())


def test_api_error_sets_last_error(tmp_path):
    async def check():
        gh = FakeGitHub(fail_api=True)
        await gh.server.start_server()
        session, rec, pub = await _make_publisher(tmp_path, gh)
        try:
            assert await pub.check_now() is None
            assert "release lookup failed" in pub.last_error
            assert rec.published == []
        finally:
            await session.close()
            await gh.server.close()
    _run(check())


def test_run_never_raises_and_loops(tmp_path):
    async def check():
        gh = FakeGitHub(fail_api=True)   # every check fails
        await gh.server.start_server()
        session, rec, pub = await _make_publisher(tmp_path, gh, interval=0.05)
        sleeps = []

        async def fake_sleep(s):
            sleeps.append(s)
            if len(sleeps) >= 3:
                raise asyncio.CancelledError()

        pub._sleep = fake_sleep
        try:
            try:
                await pub.run()   # must not raise despite failing checks
                raise AssertionError("run should have been cancelled")
            except asyncio.CancelledError:
                pass
            assert gh.api_hits >= 3
            assert pub.last_error and "release lookup failed" in pub.last_error
        finally:
            await session.close()
            await gh.server.close()
    _run(check())


# ----------------------------------------------------------------------
# host_base_url
# ----------------------------------------------------------------------

def test_host_base_url_primary_interface():
    async def check():
        sup = FakeSupervisor()
        await sup.server.start_server()
        session = aiohttp.ClientSession()
        try:
            url = await host_base_url(session, "TOKEN", 8098,
                                      supervisor=str(sup.server.make_url("")).rstrip("/"))
            assert url == "http://192.168.8.25:8098"
            assert sup.seen_auth == "Bearer TOKEN"
        finally:
            await session.close()
            await sup.server.close()
    _run(check())


def test_host_base_url_fallback_to_first_ipv4():
    async def check():
        sup = FakeSupervisor(payload={"result": "ok", "data": {"interfaces": [
            {"interface": "eth0", "ipv4": {"address": ["10.0.0.5/24"]}},
            {"interface": "wlan0", "ipv4": {"address": ["192.168.1.9/24"]}},
        ]}})
        await sup.server.start_server()
        session = aiohttp.ClientSession()
        try:
            url = await host_base_url(session, "t", 8098,
                                      supervisor=str(sup.server.make_url("")).rstrip("/"))
            assert url == "http://10.0.0.5:8098"
        finally:
            await session.close()
            await sup.server.close()
    _run(check())


def test_host_base_url_errors_return_none():
    async def check():
        session = aiohttp.ClientSession()
        try:
            # HTTP error
            sup = FakeSupervisor(status=403)
            await sup.server.start_server()
            assert await host_base_url(session, "t", 8098,
                                       supervisor=str(sup.server.make_url("")).rstrip("/")) is None
            await sup.server.close()
            # no ipv4 at all
            sup = FakeSupervisor(payload={"data": {"interfaces": [{"interface": "lo"}]}})
            await sup.server.start_server()
            assert await host_base_url(session, "t", 8098,
                                       supervisor=str(sup.server.make_url("")).rstrip("/")) is None
            await sup.server.close()
            # connection refused
            assert await host_base_url(session, "t", 8098,
                                       supervisor="http://127.0.0.1:1") is None
        finally:
            await session.close()
    _run(check())


def test_host_base_url_uses_the_published_host_port():
    """8098 inside the container published as 8093 on the host (8098 was taken)."""
    async def check():
        session = aiohttp.ClientSession()
        try:
            for network, self_status, want in (
                ({"8098/tcp": 8093}, 200, "http://192.168.8.25:8093"),
                ({"8098/tcp": None}, 200, None),                     # not published
                ({}, 200, "http://192.168.8.25:8098"),               # unknown: assume same
                ({"8098/tcp": 8093}, 403, "http://192.168.8.25:8098"),  # can't ask: assume same
            ):
                sup = FakeSupervisor(network=network, self_status=self_status)
                await sup.server.start_server()
                got = await host_base_url(session, "t", 8098,
                                          supervisor=str(sup.server.make_url("")).rstrip("/"))
                await sup.server.close()
                assert got == want, (network, self_status, got)
        finally:
            await session.close()
    _run(check())
