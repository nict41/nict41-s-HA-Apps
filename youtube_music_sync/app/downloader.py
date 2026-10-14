"""Everything that shells out to yt-dlp, plus tagging downloads afterwards."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass

from library import VIDEO_ID_TAG
from settings import Config

_LOG = logging.getLogger(__name__)

# Titles YouTube substitutes for entries that cannot be fetched at all.
UNAVAILABLE_TITLES = frozenset(
    {"[private video]", "[deleted video]", "[unavailable video]", "[age restricted]"}
)

FAILURE_TRANSIENT = "transient"
FAILURE_UNAVAILABLE = "unavailable"

# Failures meaning "this video cannot be downloaded", as opposed to "that
# attempt did not work". Only the first kind is worth backing off from: the
# second is a captcha, a rate limit or a flaky network, and retrying next
# cycle is exactly right.
#
# These are matched against yt-dlp's own last line of output, so they track
# its wording rather than YouTube's. Wording drifts; an unrecognised message
# is treated as transient, which costs a retry rather than silently retiring
# a track that was only briefly unreachable.
_TRANSIENT_PATTERNS = (
    # Checked FIRST, because some of these read like permanent failures.
    # "Sign in to confirm you're not a bot" is a rate-limit challenge, not an
    # age gate, and must never be allowed to retire a track.
    "not a bot",
    "http error 429",
    "too many requests",
    "http error 403",
    "forbidden",
    "http error 503",
    "service unavailable",
    "aria2c exited",
    "unable to download webpage",
    "unable to download api page",
    "temporary failure",
    "timed out",
    "timeout",
    "connection",
    "network",
    "read error",
    "incomplete",
    "giving up after",
)

_UNAVAILABLE_PATTERNS = (
    "video unavailable",
    "this video is unavailable",
    # Covers both "not available in your country" and yt-dlp's actual
    # wording, "has not made this video available in your country", as well
    # as "unavailable in your country".
    "available in your country",
    "private video",
    "is private",
    "has been removed",
    "removed by the uploader",
    "no longer available",
    "account associated with this video has been terminated",
    "members-only",
    "members only",
    "join this channel",
    "not available from your location",
    "who has blocked it in your country",
    "copyright",
    "age-restricted",
    "confirm your age",
    "inappropriate for some users",
)


# yt-dlp prefixes a message with its extractor and the video id:
#
#     ERROR: [youtube] aB403cdEfGh: Video unavailable
#
# An 11-character YouTube id is drawn from [A-Za-z0-9_-], so it can perfectly
# well contain "403", "429" or "timeout". Matching the patterns below against
# the id would read a dead video as a transient failure and retry it forever -
# precisely the bug this is here to end. The prefix is stripped first so only
# YouTube's actual words are classified.
_ERROR_PREFIX = re.compile(
    r"^\s*(?:ERROR|WARNING)\s*:\s*(?:\[[^\]]+\]\s*)?(?:[\w-]+\s*:\s*)?",
    re.IGNORECASE,
)


def classify_failure(error: str | None) -> str:
    """Decide whether a failed download is worth repeating next cycle.

    Returns FAILURE_UNAVAILABLE only when YouTube said something that means
    the video itself cannot be fetched. Everything else, including anything
    unrecognised, is FAILURE_TRANSIENT.
    """
    text = _ERROR_PREFIX.sub("", (error or "").strip()).lower()
    if not text:
        return FAILURE_TRANSIENT
    if any(pattern in text for pattern in _TRANSIENT_PATTERNS):
        return FAILURE_TRANSIENT
    if any(pattern in text for pattern in _UNAVAILABLE_PATTERNS):
        return FAILURE_UNAVAILABLE
    return FAILURE_TRANSIENT

# Centre-crop the artwork to a square before embedding. YouTube thumbnails are
# 16:9, which most music players letterbox or crop badly; album art is square.
# The if() guards a portrait thumbnail, and the commas inside it are escaped
# because ffmpeg treats a bare comma as the end of a filter.
_SQUARE_CROP = (
    "crop=w=if(gt(iw\\,ih)\\,ih\\,iw):h=if(gt(iw\\,ih)\\,ih\\,iw)"
)


@dataclass(frozen=True)
class PlaylistEntry:
    video_id: str
    title: str
    duration: float | None
    uploader: str
    playlist: str

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"


@dataclass(frozen=True)
class DownloadResult:
    ok: bool
    path: str | None = None
    error: str = ""
    skipped_existing: bool = False


def _common_args(cfg: Config) -> list[str]:
    args = ["--no-progress", "--newline", "--no-warnings", "--socket-timeout", "30"]
    cookies = cfg.resolved_cookies_file
    if cookies:
        args += ["--cookies", cookies]
    return args


def build_playlist_argv(cfg: Config, url: str) -> list[str]:
    """Enumerate a playlist without downloading anything.

    --flat-playlist keeps this to a single cheap request and still returns the
    per-entry duration, which is what makes it possible to recognise a track
    the library already has *before* spending bandwidth on it.
    """
    return [
        "yt-dlp",
        *_common_args(cfg),
        "--flat-playlist",
        "--dump-single-json",
        "--ignore-no-formats-error",
        url,
    ]


def build_download_argv(cfg: Config, url: str, print_file: str) -> list[str]:
    argv = [
        "yt-dlp",
        *_common_args(cfg),
        "-f", "bestaudio/best",
        "--extract-audio",
        "--audio-format", cfg.audio_format,
        "--embed-metadata",
        "--no-overwrites",
        "--retries", "3",
        "--fragment-retries", "10",
        "--download-archive", cfg.archive_path,
        "--paths", f"home:{cfg.music_dir}",
        "--paths", f"temp:{cfg.tmp_dir}",
        "-o", cfg.output_template,
        # --print-to-file implies --simulate, which would download nothing.
        "--no-simulate",
        "--print-to-file", "after_move:filepath", print_file,
    ]

    # Only meaningful when re-encoding; "best" remuxes the original stream and
    # ignores quality entirely.
    if cfg.audio_format in ("mp3", "vorbis"):
        argv += ["--audio-quality", "0"]

    if cfg.embed_thumbnail:
        # webp thumbnails are rejected by several muxers, so normalise to jpg.
        argv += ["--embed-thumbnail", "--convert-thumbnail", "jpg"]
        if cfg.crop_thumbnail_square:
            argv += [
                "--postprocessor-args",
                f"EmbedThumbnail+ffmpeg_o:-c:v mjpeg -vf {_SQUARE_CROP}",
            ]

    if cfg.sponsorblock:
        # Trims the sponsor/intro/outro talking segments off music videos so
        # the saved track starts on the music.
        argv += ["--sponsorblock-remove", "music_offtopic"]

    if cfg.rate_limit:
        argv += ["--limit-rate", cfg.rate_limit]

    if cfg.use_aria2c:
        argv += ["--downloader", "aria2c", "--downloader-args", "aria2c:-x4 -s4 -k1M"]

    # Last, so a hand-written flag can override anything chosen above.
    argv += list(cfg.extra_args)
    argv.append(url)
    return argv


def _run(argv: list[str], timeout: int) -> subprocess.CompletedProcess:
    _LOG.debug("running: %s", " ".join(argv))
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)


def parse_playlist_json(payload: str, fallback_url: str = "") -> tuple[str, list[PlaylistEntry]]:
    """Pull usable entries out of yt-dlp's --dump-single-json output."""
    data = json.loads(payload)
    playlist_title = str(data.get("title") or data.get("id") or fallback_url)

    entries: list[PlaylistEntry] = []
    for raw in data.get("entries") or []:
        if not isinstance(raw, dict):
            continue
        video_id = raw.get("id")
        title = (raw.get("title") or "").strip()
        if not video_id or not title:
            continue
        if title.lower() in UNAVAILABLE_TITLES:
            _LOG.info("Skipping unavailable entry %s (%s)", video_id, title)
            continue
        duration = raw.get("duration")
        entries.append(
            PlaylistEntry(
                video_id=str(video_id),
                title=title,
                duration=float(duration) if duration else None,
                uploader=str(raw.get("uploader") or raw.get("channel") or ""),
                playlist=playlist_title,
            )
        )
    return playlist_title, entries


def fetch_playlist(cfg: Config, url: str, timeout: int = 300) -> tuple[str, list[PlaylistEntry]]:
    """Return (playlist title, entries). Raises RuntimeError on failure."""
    result = _run(build_playlist_argv(cfg, url), timeout)
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(
            (result.stderr or result.stdout or "yt-dlp produced no output").strip().splitlines()[-1]
            if (result.stderr or result.stdout).strip()
            else "yt-dlp produced no output"
        )
    return parse_playlist_json(result.stdout, fallback_url=url)


def download(cfg: Config, entry: PlaylistEntry, timeout: int = 1800) -> DownloadResult:
    """Download one entry's audio into the library."""
    os.makedirs(cfg.tmp_dir, exist_ok=True)
    handle, print_file = tempfile.mkstemp(prefix="yms-path-", dir=cfg.tmp_dir)
    os.close(handle)
    try:
        result = _run(build_download_argv(cfg, entry.url, print_file), timeout)
        with open(print_file, "r", encoding="utf-8", errors="replace") as fh:
            printed = [line.strip() for line in fh if line.strip()]
    finally:
        try:
            os.unlink(print_file)
        except OSError:
            pass

    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()
        return DownloadResult(ok=False, error=tail[-1] if tail else f"yt-dlp exited {result.returncode}")

    if not printed:
        # Exit 0 with nothing moved: --no-overwrites found a file of the same
        # name already sitting in the library, or the archive already had it.
        return DownloadResult(
            ok=False,
            skipped_existing=True,
            error="yt-dlp wrote no file - a file of that name already exists in the library",
        )

    return DownloadResult(ok=True, path=printed[-1])


def stamp_video_id(path: str, video_id: str) -> bool:
    """Write the YouTube id into the file's tags.

    This is what makes duplicate detection survive a lost database or a
    renamed file: the id travels with the music.
    """
    try:
        from mutagen import File as MutagenFile
        from mutagen.id3 import ID3, TXXX
        from mutagen.mp4 import MP4, MP4FreeForm

        audio = MutagenFile(path)
        if audio is None:
            return False

        if isinstance(audio, MP4):
            audio[f"----:com.apple.iTunes:{VIDEO_ID_TAG}"] = [
                MP4FreeForm(video_id.encode("utf-8"))
            ]
        elif isinstance(audio.tags, ID3) or (audio.tags is None and path.lower().endswith(".mp3")):
            if audio.tags is None:
                audio.add_tags()
            audio.tags.add(TXXX(encoding=3, desc=VIDEO_ID_TAG, text=[video_id]))
        else:
            if audio.tags is None:
                audio.add_tags()
            audio[VIDEO_ID_TAG] = video_id

        audio.save()
        return True
    except Exception as err:
        _LOG.warning("Could not stamp %s onto %s: %s", VIDEO_ID_TAG, path, err)
        return False
