"""Test fixtures.

Nothing in this suite touches a real Home Assistant instance or a real
printer: every upstream is an httpx.MockTransport serving bytes this file
makes up. That is not only hygiene - the camera this app talks to accepts one
connection and crashes when something opens a second, so a test that could
reach it would be a test that could brick a printer.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

# Read before any module captures them. The app reads options lazily, so the
# tests can still monkeypatch these per test.
os.environ.setdefault("CAMERA_ENTITY", "camera.centauri_carbon_2_chamber_camera")
os.environ.setdefault("PAGE_TITLE", "Test Cam")
os.environ.setdefault("FPS", "5")
os.environ.setdefault("MAX_VIEWERS", "5")
os.environ.setdefault("ACCESS_KEY", "")
os.environ.setdefault("SUPERVISOR_TOKEN", "test-token")
os.environ.setdefault("HA_API", "http://supervisor.test/core/api")


# A one-pixel JPEG is all the fan-out ever needs: it only cares that a frame
# is bytes beginning with the JPEG start-of-image marker.
JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300"
    "0806060706050806070707090908" + "0a" * 50
) + bytes.fromhex("ffd9")


def mjpeg_body(frames, boundary="--frameboundary", with_length=True, close=False):
    """A multipart/x-mixed-replace body, spelled the way HA spells it.

    Home Assistant hands aiohttp the literal boundary `--frameboundary`, so
    the delimiter on the wire is `----frameboundary` - the default here, to
    keep the awkward case in the common path.
    """
    delimiter = b"--" + boundary.encode()
    out = bytearray()
    for frame in frames:
        out += delimiter + b"\r\n" + b"Content-Type: image/jpeg\r\n"
        if with_length:
            out += f"Content-Length: {len(frame)}\r\n".encode()
        out += b"\r\n" + frame + b"\r\n"
    if close:
        out += delimiter + b"--\r\n"
    return bytes(out)


@pytest.fixture
def _fast_timers(monkeypatch):
    """Shrink every wait so the suite runs in milliseconds, not minutes.

    Requested per module rather than autouse, because test_settings.py
    asserts on the real values - they are the ones that keep the printer
    alive, and a test suite that quietly rewrote them would be testing
    nothing. Everywhere else only the behaviour matters.
    """
    import settings

    monkeypatch.setattr(settings, "IDLE_SHUTDOWN_SECONDS", 0.08)
    monkeypatch.setattr(settings, "STALL_SECONDS", 0.25)
    monkeypatch.setattr(settings, "BACKOFF_START_SECONDS", 0.05)
    monkeypatch.setattr(settings, "BACKOFF_MAX_SECONDS", 0.4)
    yield


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.setenv("ACCESS_KEY", "")
    monkeypatch.setenv("MAX_VIEWERS", "5")
    monkeypatch.setenv("FPS", "5")
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")
    yield
