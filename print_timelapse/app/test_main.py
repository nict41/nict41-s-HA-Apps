import io
import os
import shutil
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

# DATA_DIR must be set before `main` is imported, since main.py creates its
# data directories at import time.
_TMP_DATA_DIR = tempfile.mkdtemp(prefix="timelapse-test-")
os.environ["DATA_DIR"] = _TMP_DATA_DIR
os.environ["GIF_WIDTH"] = "64"

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import main

client = TestClient(main.app)


def _jpeg_bytes(color=(255, 0, 0)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), color).save(buf, format="JPEG")
    return buf.getvalue()


_FRAME_JPEG = _jpeg_bytes()


class _ImageHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/missing.jpg":
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(_FRAME_JPEG)))
        self.end_headers()
        self.wfile.write(_FRAME_JPEG)

    def log_message(self, format, *args):
        pass


# /frame now fetches the image itself (rest_command can't upload files), so
# tests stand up a tiny local HTTP server to play the role of HA's /local/.
_image_server = HTTPServer(("127.0.0.1", 0), _ImageHandler)
threading.Thread(target=_image_server.serve_forever, daemon=True).start()
IMAGE_URL = f"http://127.0.0.1:{_image_server.server_port}/snapshot.jpg"
MISSING_IMAGE_URL = f"http://127.0.0.1:{_image_server.server_port}/missing.jpg"


@pytest.fixture(autouse=True)
def _clean_data_dir():
    yield
    shutil.rmtree(main.CURRENT_DIR, ignore_errors=True)
    shutil.rmtree(main.ARCHIVE_DIR, ignore_errors=True)
    main.CURRENT_DIR.mkdir(parents=True, exist_ok=True)
    main.ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)


def test_start_creates_empty_job_dir():
    resp = client.post("/start", data={"job_id": "job1"})
    assert resp.status_code == 200
    assert (main.CURRENT_DIR / "job1").is_dir()


def test_start_clears_existing_frames():
    client.post("/start", data={"job_id": "job1"})
    client.post(
        "/frame",
        data={"job_id": "job1", "percent": "10", "image_url": IMAGE_URL},
    )
    client.post("/start", data={"job_id": "job1"})
    assert list((main.CURRENT_DIR / "job1").glob("frame_*.jpg")) == []


@pytest.mark.parametrize("bad_job_id", ["../etc", "has space", "semi;colon"])
def test_frame_rejects_unsafe_job_id(bad_job_id):
    resp = client.post(
        "/frame",
        data={"job_id": bad_job_id, "percent": "10", "image_url": IMAGE_URL},
    )
    assert resp.status_code == 400


def test_frame_rejects_missing_job_id():
    # FastAPI's own required-field check rejects this (422) before it ever
    # reaches the job_id format validation (400).
    resp = client.post(
        "/frame",
        data={"job_id": "", "percent": "10", "image_url": IMAGE_URL},
    )
    assert resp.status_code == 422


@pytest.mark.parametrize("bad_percent", [-1, 101])
def test_frame_rejects_out_of_range_percent(bad_percent):
    resp = client.post(
        "/frame",
        data={"job_id": "job1", "percent": str(bad_percent), "image_url": IMAGE_URL},
    )
    assert resp.status_code == 400


def test_frame_returns_502_when_image_fetch_fails():
    client.post("/start", data={"job_id": "job1"})
    resp = client.post(
        "/frame",
        data={"job_id": "job1", "percent": "10", "image_url": MISSING_IMAGE_URL},
    )
    assert resp.status_code == 502


def test_frame_zero_pads_and_dedupes_same_percent():
    client.post("/start", data={"job_id": "job1"})
    client.post(
        "/frame",
        data={"job_id": "job1", "percent": "7", "image_url": IMAGE_URL},
    )
    client.post(
        "/frame",
        data={"job_id": "job1", "percent": "7", "image_url": IMAGE_URL},
    )
    frames = list((main.CURRENT_DIR / "job1").glob("frame_*.jpg"))
    assert [f.name for f in frames] == ["frame_007.jpg"]


def test_finish_without_frames_returns_404():
    client.post("/start", data={"job_id": "empty-job"})
    resp = client.post("/finish", data={"job_id": "empty-job"})
    assert resp.status_code == 404


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_finish_builds_gif_and_appears_in_gifs_and_gallery():
    client.post("/start", data={"job_id": "job2"})
    for percent in (0, 50, 100):
        client.post(
            "/frame",
            data={"job_id": "job2", "percent": str(percent), "image_url": IMAGE_URL},
        )

    resp = client.post("/finish", data={"job_id": "job2"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["filename"].startswith("job2_")
    gif_path = main.ARCHIVE_DIR / body["filename"]
    assert gif_path.exists()

    # cleanup_after_finish defaults to true.
    assert not (main.CURRENT_DIR / "job2").exists()

    assert body["exported_to"] is None  # gif_export_path unset in these tests

    listed = client.get("/gifs").json()["gifs"]
    assert any(g["filename"] == body["filename"] for g in listed)

    gallery_html = client.get("/").text
    assert f"archive/{body['filename']}" in gallery_html
    # Links must stay relative (no leading slash) so they survive being
    # proxied through Home Assistant's ingress path prefix.
    assert f'"/archive/{body["filename"]}"' not in gallery_html


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_finish_exports_gif_when_export_path_configured(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "MEDIA_DIR", tmp_path)
    monkeypatch.setattr(main, "GIF_EXPORT_PATH", "nas/timelapses")

    client.post("/start", data={"job_id": "job3"})
    client.post(
        "/frame",
        data={"job_id": "job3", "percent": "0", "image_url": IMAGE_URL},
    )

    resp = client.post("/finish", data={"job_id": "job3"})
    assert resp.status_code == 200
    body = resp.json()

    exported_path = tmp_path / "nas" / "timelapses" / body["filename"]
    assert body["exported_to"] == str(exported_path)
    assert exported_path.exists()
