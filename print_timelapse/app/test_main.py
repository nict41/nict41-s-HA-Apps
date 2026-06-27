import io
import os
import shutil
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# DATA_DIR must be set before `main` is imported, since main.py creates its
# data directories at import time.
_TMP_DATA_DIR = tempfile.mkdtemp(prefix="timelapse-test-")
os.environ["DATA_DIR"] = _TMP_DATA_DIR
os.environ["GIF_WIDTH"] = "64"

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import main
import settings

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
    # Reset any in-app settings overrides so each test starts from defaults.
    settings._SETTINGS_PATH.unlink(missing_ok=True)


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
    settings.set_many({"gif_export_path": "nas/timelapses"})

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


# ---- Settings ----------------------------------------------------------

def test_settings_defaults_then_override_roundtrips():
    assert settings.get_int("gif_fps") == settings._DEFAULTS["gif_fps"]
    settings.set_many({"gif_fps": 15})
    assert settings.get_int("gif_fps") == 15


def test_settings_clamps_integers_into_range():
    settings.set_many({"gif_width": 99999})
    assert settings.get_int("gif_width") == 1920
    settings.set_many({"gif_width": 1})
    assert settings.get_int("gif_width") == 160


def test_settings_bool_and_export_path_traversal_is_stripped():
    settings.set_many({"cleanup_after_finish": False, "gif_export_path": "/../etc/passwd/"})
    assert settings.get_bool("cleanup_after_finish") is False
    # ".." and leading/trailing slashes removed - can't escape /media.
    assert settings.get_str("gif_export_path") == "etc/passwd"


def test_settings_page_renders_current_values():
    settings.set_many({"gif_fps": 9, "gif_export_path": "myvideos"})
    html = client.get("/settings").text
    assert 'value="9"' in html
    assert "myvideos" in html


def test_post_settings_saves_and_redirects():
    resp = client.post(
        "/settings",
        data={"gif_fps": "20", "gif_width": "720", "cleanup_after_finish": "true", "gif_export_path": "vids"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert settings.get_int("gif_fps") == 20
    assert settings.get_int("gif_width") == 720
    assert settings.get_bool("cleanup_after_finish") is True
    assert settings.get_str("gif_export_path") == "vids"


def test_post_settings_unchecked_checkbox_means_false():
    resp = client.post(
        "/settings",
        data={"gif_fps": "8", "gif_width": "480", "gif_export_path": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert settings.get_bool("cleanup_after_finish") is False


# ---- Live in-progress jobs --------------------------------------------

def test_jobs_endpoint_reports_in_progress_capture():
    client.post("/start", data={"job_id": "jobX"})
    client.post("/frame", data={"job_id": "jobX", "percent": "40", "image_url": IMAGE_URL})
    jobs = client.get("/jobs").json()["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["job_id"] == "jobX"
    assert jobs[0]["frames"] == 1
    assert jobs[0]["percent"] == 40


def test_jobs_endpoint_ignores_empty_job_dirs():
    client.post("/start", data={"job_id": "emptyJob"})  # started, no frames yet
    assert client.get("/jobs").json()["jobs"] == []


def test_delete_job_removes_current_dir_and_clears_from_jobs():
    client.post("/start", data={"job_id": "stuckjob"})
    client.post("/frame", data={"job_id": "stuckjob", "percent": "100", "image_url": IMAGE_URL})
    assert (main.CURRENT_DIR / "stuckjob").is_dir()

    resp = client.post("/jobs/delete", data={"job_id": "stuckjob"})
    assert resp.status_code == 200
    assert not (main.CURRENT_DIR / "stuckjob").exists()
    assert client.get("/jobs").json()["jobs"] == []


@pytest.mark.parametrize("bad_job_id", ["../archive", "has space", "..", "a/b"])
def test_delete_job_rejects_unsafe_job_id(bad_job_id):
    resp = client.post("/jobs/delete", data={"job_id": bad_job_id})
    assert resp.status_code == 400


def test_delete_job_leaves_archive_untouched(tmp_path):
    # A real GIF in the archive must never be removed by the job-delete route.
    gif = main.ARCHIVE_DIR / "keep_me.gif"
    gif.write_bytes(b"GIF89a")
    client.post("/jobs/delete", data={"job_id": "keep_me"})  # not a current/ job
    assert gif.exists()


def test_delete_nonexistent_job_is_a_noop_ok():
    resp = client.post("/jobs/delete", data={"job_id": "ghost"})
    assert resp.status_code == 200


def test_job_records_include_latest_frame():
    client.post("/start", data={"job_id": "framejob"})
    client.post("/frame", data={"job_id": "framejob", "percent": "30", "image_url": IMAGE_URL})
    client.post("/frame", data={"job_id": "framejob", "percent": "80", "image_url": IMAGE_URL})
    jobs = client.get("/jobs").json()["jobs"]
    assert jobs[0]["latest_frame"] == "frame_080.jpg"


def test_latest_frame_route_serves_newest_frame():
    client.post("/start", data={"job_id": "framejob"})
    client.post("/frame", data={"job_id": "framejob", "percent": "10", "image_url": IMAGE_URL})
    resp = client.get("/jobs/framejob/frame")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    # It's the actual stored frame file's bytes.
    assert resp.content == (main.CURRENT_DIR / "framejob" / "frame_010.jpg").read_bytes()

    # Add a newer frame -> the route follows it.
    client.post("/frame", data={"job_id": "framejob", "percent": "90", "image_url": IMAGE_URL})
    resp2 = client.get("/jobs/framejob/frame")
    assert resp2.content == (main.CURRENT_DIR / "framejob" / "frame_090.jpg").read_bytes()


def test_latest_frame_route_404_when_no_frames():
    client.post("/start", data={"job_id": "framejob"})  # started, no frames
    assert client.get("/jobs/framejob/frame").status_code == 404
    assert client.get("/jobs/missingjob/frame").status_code == 404


@pytest.mark.parametrize("bad_job_id", ["has space", "semi;colon", "dot.dot"])
def test_latest_frame_route_rejects_unsafe_job_id(bad_job_id):
    assert client.get(f"/jobs/{bad_job_id}/frame").status_code == 400


# ---- /finish honours runtime settings ----------------------------------

def test_finish_uses_settings_fps_and_width(monkeypatch):
    settings.set_many({"gif_fps": 12, "gif_width": 320})
    recorded = {}

    def fake_run(cmd, capture_output, text):
        recorded["cmd"] = cmd
        Path(cmd[-1]).write_bytes(b"GIF89a")  # produce the output so /finish proceeds

        class _Result:
            returncode = 0
            stderr = ""

        return _Result()

    monkeypatch.setattr(main.subprocess, "run", fake_run)
    client.post("/start", data={"job_id": "jobcfg"})
    client.post("/frame", data={"job_id": "jobcfg", "percent": "0", "image_url": IMAGE_URL})

    resp = client.post("/finish", data={"job_id": "jobcfg"})
    assert resp.status_code == 200
    cmd = recorded["cmd"]
    assert "12" in cmd  # the configured framerate was passed to ffmpeg
    assert any("scale=320:" in part for part in cmd)  # the configured width
