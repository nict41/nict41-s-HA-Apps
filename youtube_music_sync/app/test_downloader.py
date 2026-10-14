import json
import subprocess

import pytest

import downloader
from downloader import PlaylistEntry, build_download_argv, build_playlist_argv, parse_playlist_json
from settings import Config


def cfg(**kwargs):
    base = dict(playlists=("u",), music_dir="/media/nas1/Music", data_dir="/data")
    base.update(kwargs)
    return Config(**base)


def test_download_argv_defaults():
    argv = build_download_argv(cfg(), "URL", "/data/tmp/p.txt")
    assert argv[0] == "yt-dlp"
    assert argv[-1] == "URL"
    # "best" means remux the original stream rather than re-encode it.
    assert "--audio-format" in argv and argv[argv.index("--audio-format") + 1] == "best"
    assert "--extract-audio" in argv
    assert "--embed-metadata" in argv
    # Without this, --print-to-file would silently simulate and download nothing.
    assert "--no-simulate" in argv
    # Without this, a same-named file already in the library would be clobbered.
    assert "--no-overwrites" in argv
    assert argv[argv.index("--paths") + 1] == "home:/media/nas1/Music"
    assert "--download-archive" in argv


def test_audio_quality_only_when_re_encoding():
    assert "--audio-quality" not in build_download_argv(cfg(), "U", "/p")
    assert "--audio-quality" in build_download_argv(cfg(audio_format="mp3"), "U", "/p")
    assert "--audio-quality" not in build_download_argv(cfg(audio_format="m4a"), "U", "/p")


def test_sponsorblock_and_thumbnail_toggles():
    on = build_download_argv(cfg(), "U", "/p")
    assert on[on.index("--sponsorblock-remove") + 1] == "music_offtopic"
    assert "--embed-thumbnail" in on
    assert any("crop=" in a for a in on)

    off = build_download_argv(cfg(sponsorblock=False, embed_thumbnail=False), "U", "/p")
    assert "--sponsorblock-remove" not in off
    assert "--embed-thumbnail" not in off
    assert not any("crop=" in a for a in off)

    no_crop = build_download_argv(cfg(crop_thumbnail_square=False), "U", "/p")
    assert "--embed-thumbnail" in no_crop
    assert not any("crop=" in a for a in no_crop)


def test_square_crop_escapes_commas_for_ffmpeg():
    argv = build_download_argv(cfg(), "U", "/p")
    crop = next(a for a in argv if "crop=" in a)
    # A bare comma would end the filter and make ffmpeg reject the whole graph.
    assert "\\," in crop
    assert "," not in crop.replace("\\,", "")


def test_optional_flags():
    rated = build_download_argv(cfg(rate_limit="1M"), "U", "/p")
    assert rated[rated.index("--limit-rate") + 1] == "1M"

    aria = build_download_argv(cfg(use_aria2c=True), "U", "/p")
    assert aria[aria.index("--downloader") + 1] == "aria2c"

    cookied = build_download_argv(cfg(cookies_file="/share/c.txt"), "U", "/p")
    assert cookied[cookied.index("--cookies") + 1] == "/share/c.txt"


def test_extra_args_come_last_so_they_can_override():
    argv = build_download_argv(cfg(extra_args=("--audio-format", "mp3")), "URL", "/p")
    assert argv[-3:] == ["--audio-format", "mp3", "URL"]


def test_playlist_argv_is_a_cheap_listing():
    argv = build_playlist_argv(cfg(), "URL")
    assert "--flat-playlist" in argv and "--dump-single-json" in argv
    assert "--extract-audio" not in argv


def test_parse_playlist_json_filters_unusable_entries():
    payload = json.dumps({
        "title": "My Playlist",
        "entries": [
            {"id": "aaaaaaaaaaa", "title": "A - B", "duration": 210.0, "channel": "C"},
            {"id": "bbbbbbbbbbb", "title": "[Private video]", "duration": None},
            {"id": "ccccccccccc", "title": "[Deleted video]"},
            {"id": "ddddddddddd", "title": ""},
            {"title": "no id at all"},
            "not a dict",
            {"id": "eeeeeeeeeee", "title": "No Duration"},
        ],
    })
    title, entries = parse_playlist_json(payload)
    assert title == "My Playlist"
    assert [e.video_id for e in entries] == ["aaaaaaaaaaa", "eeeeeeeeeee"]
    assert entries[0].duration == 210.0
    assert entries[0].uploader == "C"
    assert entries[1].duration is None
    assert entries[0].url == "https://www.youtube.com/watch?v=aaaaaaaaaaa"


def test_parse_playlist_json_falls_back_for_an_untitled_playlist():
    title, entries = parse_playlist_json('{"entries": []}', fallback_url="https://x")
    assert title == "https://x"
    assert entries == []


def _entry():
    return PlaylistEntry(video_id="aaaaaaaaaaa", title="A - B", duration=200.0,
                         uploader="C", playlist="P")


def _fake_run(returncode=0, written=None, stderr=""):
    """Stand in for the streaming runner download() uses."""
    def runner(argv, timeout, on_progress=None):
        target = argv[argv.index("--print-to-file") + 2]
        if written is not None:
            with open(target, "w", encoding="utf-8") as fh:
                fh.write(written)
        return returncode, ([stderr] if stderr else [])
    return runner


def test_download_returns_the_moved_path(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader, "_run_streaming",
                        _fake_run(0, "/media/nas1/Music/A - B.m4a\n"))
    result = downloader.download(cfg(data_dir=str(tmp_path)), _entry())
    assert result.ok and result.path == "/media/nas1/Music/A - B.m4a"


def test_download_reports_yt_dlp_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader, "_run_streaming",
                        _fake_run(1, None, "ERROR: Sign in to confirm you're not a bot"))
    result = downloader.download(cfg(data_dir=str(tmp_path)), _entry())
    assert not result.ok and not result.skipped_existing
    assert "not a bot" in result.error


def test_download_detects_an_existing_file_of_the_same_name(tmp_path, monkeypatch):
    # Exit 0 but nothing moved: --no-overwrites found the name taken.
    monkeypatch.setattr(downloader, "_run_streaming", _fake_run(0, ""))
    result = downloader.download(cfg(data_dir=str(tmp_path)), _entry())
    assert not result.ok and result.skipped_existing


def test_download_cleans_up_its_temp_file(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader, "_run_streaming", _fake_run(0, "/m/x.m4a\n"))
    config = cfg(data_dir=str(tmp_path))
    downloader.download(config, _entry())
    leftovers = list((tmp_path / "tmp").glob("yms-path-*"))
    assert leftovers == []


def test_fetch_playlist_raises_with_the_last_error_line(tmp_path, monkeypatch):
    def runner(argv, timeout):
        return subprocess.CompletedProcess(argv, 1, "", "line one\nERROR: playlist is private")
    monkeypatch.setattr(downloader, "_run", runner)
    with pytest.raises(RuntimeError, match="playlist is private"):
        downloader.fetch_playlist(cfg(), "URL")


# --------------------------------------------------- failure classification

@pytest.mark.parametrize("message", [
    "ERROR: [youtube] abc: Video unavailable",
    "ERROR: [youtube] abc: Private video. Sign in if you've been granted access to this video",
    "ERROR: [youtube] abc: This video has been removed by the uploader",
    "ERROR: [youtube] abc: Join this channel to get access to members-only content",
    "ERROR: [youtube] abc: The uploader has not made this video available in your country",
    "ERROR: [youtube] abc: This video is no longer available due to a copyright claim",
    "ERROR: [youtube] abc: Sign in to confirm your age. This video may be inappropriate for some users.",
    "ERROR: [youtube] abc: The account associated with this video has been terminated",
])
def test_unavailable_messages_are_classified_permanent(message):
    assert downloader.classify_failure(message) == downloader.FAILURE_UNAVAILABLE


@pytest.mark.parametrize("message", [
    # The one that matters most: a bot challenge reads like an age gate but is
    # a rate limit, and must never retire a track.
    "ERROR: [youtube] abc: Sign in to confirm you're not a bot. Use --cookies-from-browser",
    "ERROR: unable to download video data: HTTP Error 429: Too Many Requests",
    "ERROR: unable to download video data: HTTP Error 403: Forbidden",
    "ERROR: aria2c exited with code 22",
    "ERROR: [youtube] abc: Unable to download webpage: The read operation timed out",
    "ERROR: [youtube] abc: HTTP Error 503: Service Unavailable",
    "ERROR: Connection reset by peer",
    "",
    None,
    "ERROR: something nobody has seen before",
])
def test_transient_and_unknown_messages_are_retried(message):
    assert downloader.classify_failure(message) == downloader.FAILURE_TRANSIENT


@pytest.mark.parametrize("video_id", [
    # Real ids from a live run.
    "0K4T8JactHo", "QD1X7hosw4w", "mBiNBORoMjQ", "-ON8ot_fMKQ", "Yb3oA39uxTA",
    # Ids that happen to contain an HTTP status code or a transient keyword.
    # yt-dlp puts the id in the message, so matching it would read a dead
    # video as a retryable one and loop on it forever.
    "aB403cdEfGh", "xY429zabcDe", "q503mnopQRs", "abtimeoutxy", "x_network1",
])
def test_the_video_id_never_decides_the_classification(video_id):
    message = f"ERROR: [youtube] {video_id}: Video unavailable"
    assert downloader.classify_failure(message) == downloader.FAILURE_UNAVAILABLE


def test_a_bot_challenge_behind_an_id_prefix_is_still_transient():
    message = ("ERROR: [youtube] aB403cdEfGh: Sign in to confirm you're not a bot. "
               "Use --cookies-from-browser or --cookies")
    assert downloader.classify_failure(message) == downloader.FAILURE_TRANSIENT


# ------------------------------------------------------------------- progress

def test_download_asks_for_progress_but_the_playlist_scan_does_not():
    config = Config()
    argv = build_download_argv(config, "http://x", "/tmp/p")
    assert "--progress" in argv and "--no-progress" not in argv
    assert "--progress-template" in argv
    # The listing is a single cheap request; progress there is just noise.
    assert "--no-progress" in build_playlist_argv(config, "http://x")


def _script(tmp_path, body):
    """A stand-in for yt-dlp that writes what we tell it to."""
    path = tmp_path / "fake"
    path.write_text("#!/usr/bin/env python3\n" + body)
    path.chmod(0o755)
    return [str(path)]


def test_progress_lines_are_reported_as_they_arrive_not_at_the_end(tmp_path):
    seen = []
    argv = _script(tmp_path, f'''
import sys, time
for pct in ("10.0%", "50.0%", "100.0%"):
    print("{downloader.PROGRESS_PREFIX} " + pct + " of 200MiB at 1MiB/s ETA 00:10", flush=True)
    time.sleep(0.05)
print("ordinary output line", flush=True)
''')
    rc, tail = downloader._run_streaming(argv, timeout=30, on_progress=seen.append)
    assert rc == 0
    assert [s.split()[0] for s in seen] == ["10.0%", "50.0%", "100.0%"]
    # Progress never pollutes the text used for the error message.
    assert tail == ["ordinary output line"]


def test_the_error_tail_survives_a_flood_of_progress(tmp_path):
    argv = _script(tmp_path, f'''
import sys
for i in range(500):
    print("{downloader.PROGRESS_PREFIX} %d.0%% of 1MiB at 1MiB/s ETA 00:01" % (i % 100), flush=True)
print("ERROR: [youtube] abc: Video unavailable", flush=True)
sys.exit(1)
''')
    rc, tail = downloader._run_streaming(argv, timeout=30, on_progress=lambda _t: None)
    assert rc == 1
    assert tail[-1] == "ERROR: [youtube] abc: Video unavailable"


def test_stderr_is_captured_too(tmp_path):
    argv = _script(tmp_path, '''
import sys
print("ERROR: something on stderr", file=sys.stderr, flush=True)
sys.exit(2)
''')
    rc, tail = downloader._run_streaming(argv, timeout=30)
    assert rc == 2 and "ERROR: something on stderr" in tail


def test_a_silent_process_still_hits_the_timeout(tmp_path):
    """The timeout must not depend on output arriving - ffmpeg is quiet."""
    argv = _script(tmp_path, "import time\ntime.sleep(30)\n")
    with pytest.raises(subprocess.TimeoutExpired):
        downloader._run_streaming(argv, timeout=1)
