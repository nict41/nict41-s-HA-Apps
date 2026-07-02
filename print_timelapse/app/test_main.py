import io
import os
import shutil
import tempfile
import threading
import time
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
    # Reset the in-memory "is it working?" call tracker - module-level state
    # the rest of this fixture's filesystem cleanup doesn't touch.
    for route in main._last_call_at:
        main._last_call_at[route] = None


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


# ---- Poster thumbnails --------------------------------------------------

@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_finish_builds_poster_and_reports_it_in_gifs():
    client.post("/start", data={"job_id": "jobposter"})
    for percent in (0, 50, 100):
        client.post(
            "/frame",
            data={"job_id": "jobposter", "percent": str(percent), "image_url": IMAGE_URL},
        )

    resp = client.post("/finish", data={"job_id": "jobposter"})
    assert resp.status_code == 200
    body = resp.json()
    poster_path = main.ARCHIVE_DIR / Path(body["filename"]).with_suffix(".jpg")
    assert poster_path.exists()

    listed = client.get("/gifs").json()["gifs"]
    record = next(g for g in listed if g["filename"] == body["filename"])
    assert record["poster_url"] == f"/archive/{poster_path.name}"

    gallery_html = client.get("/").text
    assert f'data-poster="archive/{poster_path.name}"' in gallery_html


def test_gif_records_report_no_poster_for_legacy_gif_without_jpg_sibling():
    gif = main.ARCHIVE_DIR / "legacy.gif"
    gif.write_bytes(b"GIF89a")

    listed = client.get("/gifs").json()["gifs"]
    record = next(g for g in listed if g["filename"] == "legacy.gif")
    assert record["poster_url"] is None


def test_gallery_omits_data_poster_for_legacy_gif_without_jpg_sibling():
    gif = main.ARCHIVE_DIR / "legacy.gif"
    gif.write_bytes(b"GIF89a")

    gallery_html = client.get("/").text
    assert 'data-gif="archive/legacy.gif"' in gallery_html
    assert "data-poster" not in gallery_html


def test_finish_succeeds_even_when_poster_ffmpeg_fails(monkeypatch):
    def fake_run(cmd, capture_output, text):
        if "-frames:v" in cmd:
            class _FailResult:
                returncode = 1
                stderr = "synthetic poster failure"

            return _FailResult()
        Path(cmd[-1]).write_bytes(b"GIF89a")  # produce the GIF so /finish proceeds

        class _OkResult:
            returncode = 0
            stderr = ""

        return _OkResult()

    monkeypatch.setattr(main.subprocess, "run", fake_run)
    client.post("/start", data={"job_id": "jobposterfail"})
    client.post(
        "/frame",
        data={"job_id": "jobposterfail", "percent": "0", "image_url": IMAGE_URL},
    )

    resp = client.post("/finish", data={"job_id": "jobposterfail"})
    assert resp.status_code == 200
    body = resp.json()
    gif_path = main.ARCHIVE_DIR / body["filename"]
    assert gif_path.exists()
    poster_path = gif_path.with_suffix(".jpg")
    assert not poster_path.exists()

    listed = client.get("/gifs").json()["gifs"]
    record = next(g for g in listed if g["filename"] == body["filename"])
    assert record["poster_url"] is None


# ---- Regression guard: no vestigial render-blocking sentinel -----------

def test_pages_do_not_contain_vestigial_render_blocking_sentinel():
    for path in ("/", "/settings", "/help"):
        html = client.get(path).text
        assert "vt-ready" not in html
        assert 'rel="expect"' not in html


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
    recorded = []

    def fake_run(cmd, capture_output, text):
        recorded.append(cmd)
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
    cmd = next(c for c in recorded if "-framerate" in c)  # the GIF-build call, not the poster call
    assert "12" in cmd  # the configured framerate was passed to ffmpeg
    assert any("scale=320:" in part for part in cmd)  # the configured width


# ---- Help page ----------------------------------------------------------

def test_help_page_renders():
    html = client.get("/help").text
    assert "rest_command" in html


def test_help_status_starts_empty_and_updates_after_calls():
    assert client.get("/help/status").json()["last_call_at"] == {
        "start": None,
        "frame": None,
        "finish": None,
    }
    client.post("/start", data={"job_id": "statusjob"})
    last_call_at = client.get("/help/status").json()["last_call_at"]
    assert last_call_at["start"] is not None
    assert last_call_at["frame"] is None
    assert last_call_at["finish"] is None


def test_help_hostname_reports_static_default_when_unconfigured(monkeypatch):
    monkeypatch.setattr(main.ha_automations, "SUPERVISOR_TOKEN", "")
    body = client.get("/help/hostname").json()
    assert body == {"detected": False, "hostname": main.ha_automations._DEFAULT_HOSTNAME}


def test_create_automations_rejects_blank_field():
    # A whitespace-only value passes FastAPI's own required-field check (the
    # field is present) but fails the route's own post-.strip() blank check.
    resp = client.post(
        "/help/create-automations",
        data={
            "print_status_entity": "sensor.status",
            "print_progress_entity": "   ",
            "snapshot_camera_entity": "camera.printer",
            "snapshot_image_url": "http://homeassistant:8123/local/snap.jpg",
        },
    )
    assert resp.status_code == 400


def test_create_automations_persists_entities_even_when_unconfigured(monkeypatch):
    monkeypatch.setattr(main.ha_automations, "SUPERVISOR_TOKEN", "")
    resp = client.post(
        "/help/create-automations",
        data={
            "print_status_entity": "sensor.status",
            "print_progress_entity": "sensor.progress",
            "snapshot_camera_entity": "camera.printer",
            "snapshot_image_url": "http://homeassistant:8123/local/snap.jpg",
        },
    )
    assert resp.status_code == 503
    assert settings.get_str("print_status_entity") == "sensor.status"
    assert settings.get_str("snapshot_image_url") == "http://homeassistant:8123/local/snap.jpg"


def test_create_automations_succeeds_when_configured(monkeypatch):
    monkeypatch.setattr(main.ha_automations, "SUPERVISOR_TOKEN", "test-token")
    monkeypatch.setattr(main.ha_automations, "_request", lambda *a, **k: {})
    resp = client.post(
        "/help/create-automations",
        data={
            "print_status_entity": "sensor.status",
            "print_progress_entity": "sensor.progress",
            "snapshot_camera_entity": "camera.printer",
            "snapshot_image_url": "http://homeassistant:8123/local/snap.jpg",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is True
    assert all(body["results"].values())


# ---- Manual finish + auto-cancel stale jobs -----------------------------

def _seed_job(job_id: str, mtime_offset: float = 0.0) -> Path:
    """Create a job dir with one frame and optionally backdate its mtime."""
    job_dir = main.CURRENT_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    frame = job_dir / "frame_001.jpg"
    frame.write_bytes(_FRAME_JPEG)
    if mtime_offset:
        old = time.time() + mtime_offset
        os.utime(job_dir, (old, old))
    return job_dir


def test_auto_finish_builds_cancelled_gif_for_stale_job(monkeypatch):
    _seed_job("stalej", mtime_offset=-(main._STALE_THRESHOLD_SECONDS + 1))

    def fake_run(cmd, capture_output, text):
        # Write a sentinel file so the GIF path actually exists.
        Path(cmd[-1]).write_bytes(b"GIF89a")
        class _Ok:
            returncode = 0
            stderr = ""
        return _Ok()

    monkeypatch.setattr(main.subprocess, "run", fake_run)
    main._auto_finish_stale_jobs_once()

    gifs = list(main.ARCHIVE_DIR.glob("*.gif"))
    assert len(gifs) == 1
    assert "_cancelled_" in gifs[0].name
    assert gifs[0].name.startswith("stalej_cancelled_")


def test_auto_finish_ignores_recent_job(monkeypatch):
    _seed_job("recentj", mtime_offset=-60)  # only 1 minute old

    called = []
    monkeypatch.setattr(main.subprocess, "run", lambda *a, **k: called.append(a))
    main._auto_finish_stale_jobs_once()

    assert called == []
    assert list(main.ARCHIVE_DIR.glob("*.gif")) == []


def test_auto_finish_does_not_record_finish_call(monkeypatch):
    _seed_job("stalecall", mtime_offset=-(main._STALE_THRESHOLD_SECONDS + 1))

    def fake_run(cmd, capture_output, text):
        Path(cmd[-1]).write_bytes(b"GIF89a")
        class _Ok:
            returncode = 0
            stderr = ""
        return _Ok()

    monkeypatch.setattr(main.subprocess, "run", fake_run)
    main._auto_finish_stale_jobs_once()

    assert main._last_call_at["finish"] is None


def test_auto_finish_survives_ffmpeg_failure(monkeypatch):
    job_dir = _seed_job("failstale", mtime_offset=-(main._STALE_THRESHOLD_SECONDS + 1))

    def fake_run(cmd, capture_output, text):
        class _Fail:
            returncode = 1
            stderr = "synthetic failure"
        return _Fail()

    monkeypatch.setattr(main.subprocess, "run", fake_run)
    main._auto_finish_stale_jobs_once()  # must not raise

    assert list(main.ARCHIVE_DIR.glob("*.gif")) == []
    assert job_dir.exists()  # frames untouched since build failed before cleanup


def test_gallery_renders_finish_button_in_job_rows():
    client.post("/start", data={"job_id": "btnjob"})
    client.post("/frame", data={"job_id": "btnjob", "percent": "10", "image_url": IMAGE_URL})
    html = client.get("/").text
    assert "job-finish" in html
    assert 'data-job-id="btnjob"' in html
