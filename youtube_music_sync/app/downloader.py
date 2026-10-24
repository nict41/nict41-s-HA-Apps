"""Everything that shells out to yt-dlp, plus tagging downloads afterwards."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
from collections import deque
from collections.abc import Callable
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


# A YouTube bot/rate-limit challenge means every subsequent request this
# cycle will fail the same way - continuing only makes the block worse. This
# is checked separately from classify_failure because it drives a cycle-wide
# pause rather than a per-video backoff.
_BOT_BLOCK_PATTERNS = (
    "sign in to confirm you",
    "confirm you're not a bot",
    "http error 429",
    "too many requests",
)


def is_bot_block(error: str | None) -> bool:
    """True when yt-dlp's failure means YouTube is challenging/rate-limiting us."""
    text = _ERROR_PREFIX.sub("", (error or "").strip()).lower()
    return any(pattern in text for pattern in _BOT_BLOCK_PATTERNS)

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
    # The configured URL the entry was read from, for playlist_settings.
    source: str = ""

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"


@dataclass(frozen=True)
class DownloadResult:
    ok: bool
    path: str | None = None
    error: str = ""
    skipped_existing: bool = False
    artists: tuple[str, ...] = ()


# Marks the progress lines this add-on asks yt-dlp to emit, so they can be
# told apart from everything else on the same stream and never mistaken for
# the error tail.
PROGRESS_PREFIX = "[yms-progress]"

PROGRESS_TEMPLATE = (
    f"download:{PROGRESS_PREFIX} %(progress._percent_str)s of "
    "%(progress._total_bytes_str)s at %(progress._speed_str)s "
    "ETA %(progress._eta_str)s"
)


def _common_args(cfg: Config, *, progress: bool = False) -> list[str]:
    args = ["--newline", "--no-warnings", "--socket-timeout", "30"]
    args.append("--progress" if progress else "--no-progress")
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


def build_download_argv(
    cfg: Config, url: str, print_file: str, artists_file: str | None = None,
    dest_dir: str | None = None,
) -> list[str]:
    argv = [
        "yt-dlp",
        *_common_args(cfg, progress=True),
        "--progress-template", PROGRESS_TEMPLATE,
        "-f", "bestaudio/best",
        "--extract-audio",
        "--audio-format", cfg.audio_format,
        "--embed-metadata",
        "--no-overwrites",
        # A stale .part/.aria2 file left behind by an earlier interrupted
        # attempt (aria2c crashing, the container restarting mid-download)
        # makes the next attempt try to resume a byte range the server has
        # since stopped honouring, which fails every time with HTTP 416.
        # These are small music files - resuming saves little, and never
        # trying to buys outright immunity to that whole failure mode.
        "--no-continue",
        "--retries", "3",
        "--fragment-retries", "10",
        "--download-archive", cfg.archive_path,
        "--paths", f"home:{dest_dir or cfg.music_dir}",
        "--paths", f"temp:{cfg.tmp_dir}",
        "-o", cfg.output_template,
        # --print-to-file implies --simulate, which would download nothing.
        "--no-simulate",
        "--print-to-file", "after_move:filepath", print_file,
    ]

    if artists_file:
        # --embed-metadata joins every credited artist into one "A, B" string,
        # which a library then files as a single made-up artist. The list
        # itself is printed here so split_artist_tags can write it back as
        # one tag per artist.
        argv += ["--print-to-file", "after_move:%(artists)j", artists_file]

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


# Lines of output kept for the error message. Only the tail is needed, and a
# long download emits far too much to hold all of it.
_TAIL_LINES = 30


def _run_streaming(
    argv: list[str],
    timeout: int,
    on_progress: Callable[[str], None] | None = None,
) -> tuple[int, list[str]]:
    """Run yt-dlp, reporting progress while it works.

    subprocess.run captures everything and hands it over at the end, which is
    fine for a three-minute track and useless for a three-hour one: the log
    says nothing for however long it takes. Here the output is read as it
    arrives, progress lines go to `on_progress`, and the rest is kept for the
    error message.

    stderr is merged into stdout so one reader drains both and cannot
    deadlock on a full pipe. The reader runs on its own thread so the timeout
    is enforced even while the process is silent - during a long ffmpeg
    post-process, no output arrives at all.
    """
    _LOG.debug("running: %s", " ".join(argv))
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        errors="replace",
    )
    tail: deque[str] = deque(maxlen=_TAIL_LINES)

    def read() -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip()
            if not line:
                continue
            if line.startswith(PROGRESS_PREFIX):
                if on_progress is not None:
                    on_progress(line[len(PROGRESS_PREFIX):].strip())
                continue
            tail.append(line)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        returncode = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise
    finally:
        # Let the reader drain what is left, but never hang on it.
        reader.join(timeout=10)
        if proc.stdout is not None:
            proc.stdout.close()
    return returncode, list(tail)


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


def clear_tmp_dir(tmp_dir: str) -> int:
    """Remove every leftover file in tmp_dir. Returns how many were removed.

    Downloads happen one at a time, so whenever this runs - at startup,
    before nothing has run yet, or right before retrying a single download -
    nothing in here can belong to work still in progress. A stale .part or
    .aria2 file left by an earlier interrupted attempt is exactly what makes
    a resumed request collide with a server that has since stopped
    honouring that byte range (HTTP 416).
    """
    removed = 0
    if not os.path.isdir(tmp_dir):
        return removed
    for name in os.listdir(tmp_dir):
        path = os.path.join(tmp_dir, name)
        try:
            if os.path.isfile(path) or os.path.islink(path):
                os.unlink(path)
            else:
                shutil.rmtree(path, ignore_errors=True)
            removed += 1
        except OSError:
            pass
    return removed


def download(
    cfg: Config,
    entry: PlaylistEntry,
    timeout: int | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> DownloadResult:
    """Download one entry's audio, retrying once if it hits a stale-partial 416.

    --no-continue (above) already stops yt-dlp resuming from a leftover
    partial file in the first place, but a 416 can in principle still come
    from something else holding a stale range server-side. Belt and braces:
    on a 416, clear the temp dir and try exactly once more before giving up.
    """
    result = _attempt_download(cfg, entry, timeout, on_progress)
    if not result.ok and not result.skipped_existing and "416" in result.error:
        _LOG.warning("  %r failed with HTTP 416 (stale partial download) - "
                     "clearing temp files and retrying once.", entry.title)
        clear_tmp_dir(cfg.tmp_dir)
        result = _attempt_download(cfg, entry, timeout, on_progress)
    return result


def _attempt_download(
    cfg: Config,
    entry: PlaylistEntry,
    timeout: int | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> DownloadResult:
    """Download one entry's audio into the library.

    `timeout` covers the whole job - fetch, SponsorBlock trim, audio extract
    and thumbnail embed - not just the transfer. A long track can spend far
    more time in ffmpeg than on the wire, so it comes from the add-on's own
    option rather than a constant.
    """
    if timeout is None:
        timeout = max(cfg.download_timeout_minutes, 1) * 60
    os.makedirs(cfg.tmp_dir, exist_ok=True)
    handle, print_file = tempfile.mkstemp(prefix="yms-path-", dir=cfg.tmp_dir)
    os.close(handle)
    handle, artists_file = tempfile.mkstemp(prefix="yms-artists-", dir=cfg.tmp_dir)
    os.close(handle)
    try:
        returncode, tail = _run_streaming(
            build_download_argv(cfg, entry.url, print_file, artists_file,
                                cfg.dest_dir(entry.source, entry.playlist)),
            timeout, on_progress
        )
        with open(print_file, "r", encoding="utf-8", errors="replace") as fh:
            printed = [line.strip() for line in fh if line.strip()]
        with open(artists_file, "r", encoding="utf-8", errors="replace") as fh:
            artists = _parse_artists([line.strip() for line in fh if line.strip()])
    finally:
        for path in (print_file, artists_file):
            try:
                os.unlink(path)
            except OSError:
                pass

    if returncode != 0:
        return DownloadResult(ok=False, error=tail[-1] if tail else f"yt-dlp exited {returncode}")

    if not printed:
        # Exit 0 with nothing moved: --no-overwrites found a file of the same
        # name already sitting in the library, or the archive already had it.
        return DownloadResult(
            ok=False,
            skipped_existing=True,
            error="yt-dlp wrote no file - a file of that name already exists in the library",
        )

    return DownloadResult(ok=True, path=printed[-1], artists=artists)


def _parse_artists(lines: list[str]) -> tuple[str, ...]:
    """The artist list yt-dlp printed, or () when it had none.

    Videos with no music metadata print "NA" rather than a JSON list.
    """
    if not lines:
        return ()
    try:
        value = json.loads(lines[-1])
    except ValueError:
        return ()
    if not isinstance(value, list):
        return ()
    names = [str(v).strip() for v in value if isinstance(v, str) and v.strip()]
    return tuple(dict.fromkeys(names))


def split_artist_tags(path: str, artists: tuple[str, ...]) -> bool:
    """Replace the joined "A, B" artist tag with one tag per artist.

    Music Assistant (and most other libraries) read a multi-value artist tag
    as separate artists, but never split a single value on commas because
    real names contain them. Only touches a tag that is exactly yt-dlp's
    joined form, so a hand-edited artist is left alone.
    """
    if len(artists) < 2:
        return False
    joined = ", ".join(artists)
    try:
        from mutagen import File as MutagenFile
        from mutagen.id3 import ID3, TPE1
        from mutagen.mp4 import MP4

        audio = MutagenFile(path)
        if audio is None or audio.tags is None:
            return False

        if isinstance(audio, MP4):
            if list(audio.tags.get("\xa9ART", [])) != [joined]:
                return False
            audio.tags["\xa9ART"] = list(artists)
        elif isinstance(audio.tags, ID3):
            frame = audio.tags.get("TPE1")
            if frame is None or list(frame.text) != [joined]:
                return False
            audio.tags.add(TPE1(encoding=3, text=list(artists)))
        else:
            if list(audio.tags.get("artist", [])) != [joined]:
                return False
            audio.tags["artist"] = list(artists)

        audio.save()
        return True
    except Exception as err:
        _LOG.warning("Could not split the artist tag on %s: %s", path, err)
        return False


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


def set_genre(path: str, genre: str) -> bool:
    """Replace the genre tag. yt-dlp writes YouTube's video category there
    ("Music", "Entertainment", "Education"), which is no use as a genre."""
    if not genre:
        return False
    try:
        from mutagen import File as MutagenFile

        audio = MutagenFile(path, easy=True)
        if audio is None:
            return False
        if audio.tags is None:
            audio.add_tags()
        audio.tags["genre"] = [genre]
        audio.save()
        return True
    except Exception as err:
        _LOG.warning("Could not set the genre on %s: %s", path, err)
        return False
