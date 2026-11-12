"""Offer HA Tubes firmware updates to the clock over MQTT.

The clock firmware reads a retained message on clock/<mqtt_id>/ota/latest and
only accepts plain http:// URLs up to 127 characters, so this module downloads
the release .bin from GitHub itself and serves it from the app's own HTTP port
(api.py serves /firmware/<name> from the data dir via FirmwarePublisher.file_path).

check_now() looks at the GitHub "latest release" of the firmware repo, downloads
the matching asset once (with an ETag so unchanged releases are cheap), prunes
old files and publishes the OTA payload to every clock.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from typing import Awaitable, Callable

import aiohttp

_LOG = logging.getLogger(__name__)

GITHUB_HEADERS = {"User-Agent": "tube-display", "Accept": "application/vnd.github+json"}
MAX_FIRMWARE_BYTES = 4 * 1024 * 1024
KEEP_BINS = 2
# only these names may be served back over HTTP - no path traversal
_BIN_NAME_RE = re.compile(r"^firmware-[A-Za-z0-9_.-]+\.bin$")


class FirmwarePublisher:
    """Checks GitHub for a new HA Tubes release and publishes it to the clocks.

    `publish(topic, payload)` publishes a retained MQTT message (caller's job),
    `clock_ids()` returns the mqtt ids of the clocks, `base_url()` the app's own
    address (e.g. "http://192.168.8.25:8098") or None if unknown.
    """

    def __init__(self, session: aiohttp.ClientSession, data_dir: str,
                 publish: Callable[[str, str], Awaitable[None]],
                 clock_ids: Callable[[], list[str]],
                 base_url: Callable[[], Awaitable[str | None]],
                 repo: str = "nict41/HA-Tubes", variant: str = "ipstube",
                 api_base: str = "https://api.github.com",
                 interval: float = 6 * 3600,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self._session = session
        self._data_dir = data_dir
        self._publish = publish
        self._clock_ids = clock_ids
        self._base_url = base_url
        self._repo = repo
        self._variant = variant
        self._api_base = api_base.rstrip("/")
        self._interval = interval
        self._sleep = sleep
        self._etag: str | None = None
        self.latest: dict | None = None
        self.last_error: str | None = None

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    async def check_now(self) -> dict | None:
        """One check: returns the payload published, or None (last_error set)."""
        try:
            release = await self._fetch_latest_release()
        except Exception as err:
            self.last_error = f"release lookup failed: {err}"
            _LOG.warning("Firmware: %s", self.last_error)
            return None
        if release is None:
            # 304 Not Modified: republish the retained payload, it's cheap
            if self.latest is not None:
                await self._publish_all(self.latest)
                return self.latest
            self.last_error = "no previous release known"
            return None

        tag = release.get("tag_name") or ""
        version = tag[1:] if tag.startswith("v") else tag
        assets = release.get("assets") or []
        by_name = {a.get("name"): a for a in assets if isinstance(a, dict)}
        asset = by_name.get(f"firmware-{self._variant}-{version}.bin") \
            or by_name.get(f"firmware-{self._variant}.bin")
        if asset is None or not version:
            self.last_error = f"no firmware-{self._variant}-{version}.bin asset in {tag}"
            _LOG.warning("Firmware: %s", self.last_error)
            return None
        name = asset["name"]

        try:
            md5 = await self._ensure_file(name, asset.get("browser_download_url") or "")
        except Exception as err:
            self.last_error = f"download failed: {err}"
            _LOG.warning("Firmware: %s", self.last_error)
            return None
        self._prune()

        base = await self._base_url()
        if base is None:
            self.last_error = "app address unknown"
            _LOG.warning("Firmware: %s", self.last_error)
            return None
        url = f"{base.rstrip('/')}/firmware/{name}"
        # the firmware only takes plain http:// and at most 127 characters
        if not url.startswith("http://") or len(url) > 127:
            self.last_error = f"url not usable by the clock: {url!r}"
            _LOG.warning("Firmware: %s", self.last_error)
            return None

        payload = {
            "version": version,
            "url": url,
            "md5": md5,
            "release_url": release.get("html_url") or "",
            "title": f"HA Tubes {version}",
        }
        self.latest = payload
        self.last_error = None
        await self._publish_all(payload)
        _LOG.info("Firmware: published %s to %d clock(s)", version, len(self._clock_ids()))
        return payload

    async def run(self) -> None:
        """check_now() now, then every `interval` seconds; never raises."""
        while True:
            try:
                await self.check_now()
            except Exception:
                _LOG.exception("Firmware: check failed")
            try:
                await self._sleep(self._interval)
            except asyncio.CancelledError:
                raise

    def file_path(self, name: str) -> str | None:
        """Absolute path of a served firmware file, or None if not one of ours."""
        if not isinstance(name, str) or not _BIN_NAME_RE.match(name):
            return None
        path = os.path.join(self._data_dir, name)
        if os.path.isfile(path):
            return os.path.abspath(path)
        return None

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    async def _publish_all(self, payload: dict) -> None:
        text = json.dumps(payload)
        for clock_id in self._clock_ids():
            await self._publish(f"clock/{clock_id}/ota/latest", text)

    async def _fetch_latest_release(self) -> dict | None:
        """GET the latest release; None on 304 (unchanged)."""
        headers = dict(GITHUB_HEADERS)
        if self._etag:
            headers["If-None-Match"] = self._etag
        url = f"{self._api_base}/repos/{self._repo}/releases/latest"
        async with self._session.get(url, headers=headers,
                                     timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status == 304:
                return None
            if resp.status >= 400:
                body = await resp.text()
                raise RuntimeError(f"GitHub {resp.status}: {body[:200]}")
            etag = resp.headers.get("ETag")
            if etag:
                self._etag = etag
            return await resp.json()

    async def _ensure_file(self, name: str, download_url: str) -> str:
        """Download the asset if missing; returns its md5 (from the sidecar)."""
        path = os.path.join(self._data_dir, name)
        md5_path = path + ".md5"
        if os.path.isfile(path) and os.path.isfile(md5_path):
            with open(md5_path, encoding="ascii") as f:
                return f.read().strip()
        if not download_url:
            raise RuntimeError("asset has no download url")
        os.makedirs(self._data_dir, exist_ok=True)
        part = path + ".part"
        digest = hashlib.md5()
        try:
            # allow_redirects: browser_download_url redirects to the CDN
            async with self._session.get(download_url,
                                         timeout=aiohttp.ClientTimeout(total=120)) as resp:
                if resp.status >= 400:
                    raise RuntimeError(f"download {resp.status}")
                with open(part, "wb") as f:
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        digest.update(chunk)
                        f.write(chunk)
                        if f.tell() > MAX_FIRMWARE_BYTES:
                            raise RuntimeError(f"firmware larger than {MAX_FIRMWARE_BYTES} bytes")
        except Exception:
            try:
                os.remove(part)
            except OSError:
                pass
            raise
        os.replace(part, path)
        md5 = digest.hexdigest()
        with open(md5_path, "w", encoding="ascii") as f:
            f.write(md5)
        return md5

    def _prune(self) -> None:
        """Keep only the newest KEEP_BINS .bin files (and their .md5 sidecars)."""
        try:
            entries = [n for n in os.listdir(self._data_dir)
                       if _BIN_NAME_RE.match(n) and os.path.isfile(os.path.join(self._data_dir, n))]
        except OSError:
            return
        entries.sort(key=lambda n: os.path.getmtime(os.path.join(self._data_dir, n)),
                     reverse=True)
        for old in entries[KEEP_BINS:]:
            for suffix in ("", ".md5"):
                try:
                    os.remove(os.path.join(self._data_dir, old + suffix))
                except OSError:
                    pass
            _LOG.info("Firmware: pruned old %s", old)


async def host_base_url(session: aiohttp.ClientSession, token: str, port: int,
                        supervisor: str = "http://supervisor") -> str | None:
    """The app's own base URL from the Supervisor's network info: the primary
    interface's IPv4 address with the port, e.g. "http://192.168.8.25:8098".
    None on any error."""
    try:
        async with session.get(f"{supervisor.rstrip('/')}/network/info",
                               headers={"Authorization": f"Bearer {token}"},
                               timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status >= 400:
                _LOG.warning("host_base_url: network/info -> %s", resp.status)
                return None
            data = await resp.json()
        interfaces = (data.get("data") or {}).get("interfaces") or []
        primary = None
        first_ipv4 = None
        for iface in interfaces:
            if not isinstance(iface, dict):
                continue
            addresses = ((iface.get("ipv4") or {}).get("address") or [])
            ip = None
            for entry in addresses:
                if isinstance(entry, str) and "/" in entry:
                    ip = entry.split("/", 1)[0]
                    break
            if ip is None:
                continue
            if first_ipv4 is None:
                first_ipv4 = ip
            if iface.get("primary"):
                primary = ip
                break
        ip = primary or first_ipv4
        if not ip:
            return None
        return f"http://{ip}:{port}"
    except Exception as err:
        _LOG.warning("host_base_url: %s", err)
        return None
