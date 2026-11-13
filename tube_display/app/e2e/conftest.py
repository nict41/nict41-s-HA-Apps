"""pytest fixtures for the editor's browser e2e tests (test_e2e_editor.py).

Two ways to run them:

  - in CI / locally with a local browser: just `pytest e2e` - Playwright launches
    its own Chromium and goes straight to the stub server's URL.
  - locally against a REMOTE Chromium (e.g. a browser on another machine or in
    a container): start that Chromium with --remote-debugging-port=9222 and set
        E2E_CDP_URL=http://localhost:9222
    Then Playwright connects over CDP and every request the page makes for
    http://editor.test/* is routed (page.route) to the local stub server: the
    fetch is done from Python (urllib) and status/headers/body are returned to
    the browser. The page itself is loaded from http://editor.test/ so the
    origin is stable no matter where the browser runs.

Every test runs at two viewport sizes (desktop 1280x900, phone 390x844) via the
`viewport` fixture param. Console errors and page errors are collected on the
`page` fixture and any of them fails the test; on failure a screenshot is saved
to e2e/artifacts/ (uploaded as a CI artifact).

Playwright is only imported inside fixtures, so this file (and the whole e2e
folder) can be byte-compiled and collected without Playwright installed; the
tests skip with a clear message instead.
"""

from __future__ import annotations

import os
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

E2E_DIR = Path(__file__).resolve().parent
APP_DIR = E2E_DIR.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
if str(E2E_DIR) not in sys.path:
    sys.path.insert(0, str(E2E_DIR))

import stub_server as stub  # noqa: E402  (needs the sys.path tweak above)

# The origin the page is loaded from when the browser is remote (CDP). All its
# requests are routed to the local stub server by the `page` fixture.
CDP_ORIGIN = "http://editor.test"

VIEWPORTS = {
    "desktop": {"width": 1280, "height": 900},
    "phone": {"width": 390, "height": 844},
}

ARTIFACTS = E2E_DIR / "artifacts"


def _playwright():
    """Import playwright.sync_api, or None when it is not installed."""
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except ImportError:
        return None


@pytest.fixture(scope="session")
def server(tmp_path_factory):
    """One stub server for the whole module run: the real editor app with the
    stub HA and the two fake clocks, on a temp copy of fixtures/screens.yaml."""
    yield from stub.stub_server_fixture(tmp_path_factory)


@pytest.fixture()
def screens_file(server):
    """Reset the stub's screens file to the fixture before each test, so every
    test starts from the same 4 screens (and a clean save history)."""
    shutil.copyfile(stub.FIXTURE, server.screens_file)
    history_dir = Path(server.screens_file).parent / ".history"
    if history_dir.is_dir():
        shutil.rmtree(history_dir)
    return server.screens_file


@pytest.fixture(params=["desktop", "phone"], ids=["desktop", "phone"])
def viewport(request):
    """The viewport size each test runs at: desktop 1280x900 and phone 390x844."""
    return VIEWPORTS[request.param]


@pytest.fixture()
def browser_context(server, viewport):
    """A Playwright browser context at the requested viewport.

    With E2E_CDP_URL set, connects to the remote Chromium over CDP; otherwise
    launches a local Chromium. Yields (playwright_ctx, base_url, remote) where
    base_url is the URL the page is loaded from and remote is True for CDP.
    """
    pw = _playwright()
    if pw is None:
        pytest.skip("playwright is not installed (pip install -r e2e/requirements-e2e.txt "
                    "&& python -m playwright install chromium)")
    cdp_url = os.environ.get("E2E_CDP_URL", "").strip()
    remote = bool(cdp_url)
    base_url = CDP_ORIGIN if remote else server.base_url
    with pw() as p:
        if remote:
            browser = p.chromium.connect_over_cdp(cdp_url)
        else:
            browser = p.chromium.launch()
        context = browser.new_context(viewport=viewport)
        yield context, base_url, remote
        context.close()
        if not remote:
            browser.close()


@pytest.fixture()
def page(browser_context, server, viewport, request):
    """A page on the editor, with console/page errors collected (any of them
    fails the test) and, when the browser is remote (E2E_CDP_URL), all
    http://editor.test/* requests routed to the local stub server."""
    context, base_url, remote = browser_context
    page = context.new_page()

    errors: list[str] = []
    page.on("console", lambda msg: errors.append(f"console.error: {msg.text}")
            if msg.type == "error" else None)
    page.on("pageerror", lambda err: errors.append(f"pageerror: {err}"))
    # name the URL behind a "Failed to load resource" console error
    page.on("response", lambda resp: errors.append(f"HTTP {resp.status}: {resp.request.method} {resp.url}")
            if resp.status == 404 else None)

    # the page has no favicon; answer the browser's /favicon.ico with 204 so the
    # 404 does not show up as a console error (every console error fails a test)
    page.route("**/favicon.ico",
               lambda route: route.fulfill(status=204, body=b""))

    if remote:
        _route_to_stub(page, server.base_url)

    # viewport: set_viewport_size works on a launched browser; on a CDP-connected
    # one it may be ignored, so also send Emulation.setDeviceMetricsOverride
    page.set_viewport_size(viewport)
    try:
        session = context.new_cdp_session(page)
        session.send("Emulation.setDeviceMetricsOverride", {
            "width": viewport["width"], "height": viewport["height"],
            "deviceScaleFactor": 1, "mobile": viewport["width"] < 600,
        })
    except Exception:
        pass  # not a Chromium CDP session (local launch): set_viewport_size is enough

    test_failed = False
    try:
        yield page, base_url
    except BaseException:
        test_failed = True
        raise
    finally:
        if test_failed:
            # screenshot of the failure (the CI job uploads e2e/artifacts)
            ARTIFACTS.mkdir(exist_ok=True)
            name = f"{request.node.name.replace('[', '-').replace(']', '')}.png"
            try:
                page.screenshot(path=str(ARTIFACTS / name), full_page=True)
            except Exception:
                pass
        if errors:
            raise AssertionError("browser errors during the test:\n" + "\n".join(errors))


def _route_to_stub(page, stub_base: str) -> None:
    """Route every http://editor.test/* request the page makes to the local
    stub server: fetch it from Python (urllib) and fulfill with the same
    status / headers / body, so a remote browser can run the tests against a
    server on this machine."""

    def handler(route):
        url = route.request.url
        assert url.startswith(CDP_ORIGIN + "/") or url == CDP_ORIGIN, url
        target = stub_base + url[len(CDP_ORIGIN):]
        req = urllib.request.Request(
            target,
            data=route.request.post_data_buffer,
            method=route.request.method,
            headers={k: v for k, v in route.request.headers.items()
                     if k.lower() not in ("host", "content-length", "connection")},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read()
                headers = {k: v for k, v in resp.headers.items()
                           if k.lower() not in ("content-encoding", "transfer-encoding",
                                                "connection")}
                route.fulfill(status=resp.status, headers=headers, body=body)
        except urllib.error.HTTPError as err:
            body = err.read()
            headers = {k: v for k, v in err.headers.items()
                       if k.lower() not in ("content-encoding", "transfer-encoding",
                                            "connection")}
            route.fulfill(status=err.code, headers=headers, body=body)
        except Exception as err:  # noqa: BLE001 - fail the request loudly
            route.fulfill(status=502, content_type="text/plain",
                          body=f"stub route failed: {err}")

    page.route(CDP_ORIGIN + "/**", handler)
