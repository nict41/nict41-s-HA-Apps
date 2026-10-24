"""YouTube Music Sync - watch public playlists, mirror new audio into a library.

One cycle: index the library, enumerate each playlist, decide per entry whether
it is already owned, download what is not, then verify the downloads against
the library again now that their real durations are known.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

import albumartist
import downloader
import settings
from library import AMBIGUOUS, DIFFERENT_VERSION, DUPLICATE, LibraryEntry, LibraryIndex, read_file_metadata
from mqtt_client import Publisher
from scanner import build_index
from settings import Config
from state import (
    STATUS_DUPLICATE_AFTER_DOWNLOAD,
    STATUS_FAILED,
    STATUS_OK,
    Store,
)

_LOG = logging.getLogger("youtube_music_sync")

# bashio log levels -> Python levels.
_LEVELS = {
    "trace": logging.DEBUG, "debug": logging.DEBUG, "info": logging.INFO,
    "notice": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR,
    "fatal": logging.CRITICAL,
}

_stop = threading.Event()


def _handle_signal(signum, _frame) -> None:
    _LOG.info("Signal %s received - stopping after the current step.", signum)
    _stop.set()


@dataclass
class CycleReport:
    planned: int = 0
    downloaded: int = 0
    duplicates: int = 0
    duplicates_after_download: int = 0
    near_misses: int = 0
    filtered: int = 0
    collisions: int = 0
    failures: int = 0
    playlist_errors: int = 0
    deferred: int = 0
    would_download: list[str] = field(default_factory=list)

    @property
    def problem(self) -> bool:
        return bool(self.failures or self.playlist_errors)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _unique_path(path: str) -> str:
    """A non-colliding variant of `path`, by appending " (2)", " (3)"..."""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    counter = 2
    while os.path.exists(f"{stem} ({counter}){ext}"):
        counter += 1
    return f"{stem} ({counter}){ext}"


def _dispose_duplicate(cfg: Config, path: str) -> tuple[str, str | None]:
    """Deal with a file that turned out to duplicate the library.

    Default is quarantine rather than delete: the matcher is good, not
    infallible, and a wrongly-judged track sitting in a folder can be put back,
    where a deleted one has to be fetched again.
    """
    if cfg.on_duplicate == "keep":
        return "kept in place", path
    if cfg.on_duplicate == "delete":
        try:
            os.remove(path)
            return "deleted", None
        except OSError as err:
            _LOG.warning("Could not delete duplicate %s: %s", path, err)
            return f"left in place (delete failed: {err})", path

    try:
        os.makedirs(cfg.quarantine_dir, exist_ok=True)
        target = _unique_path(os.path.join(cfg.quarantine_dir, os.path.basename(path)))
        shutil.move(path, target)
        return f"moved to {target}", target
    except OSError as err:
        _LOG.warning("Could not quarantine duplicate %s: %s", path, err)
        return f"left in place (quarantine failed: {err})", path


def _plan_cycle(
    cfg: Config, store: Store, index: LibraryIndex, entries: list[downloader.PlaylistEntry],
    *, record: bool,
) -> tuple[list[tuple[downloader.PlaylistEntry, object]], CycleReport]:
    """Decide what to download, without downloading anything."""
    report = CycleReport()
    already = store.downloaded_ids()
    # Videos YouTube has said are unavailable, backed off so they are not
    # re-read every cycle. They are skipped before the plan is built, so they
    # cost no max_downloads_per_cycle slot and no request.
    deferred = store.deferred_ids()
    retired = store.retired_ids()
    plan: list[tuple[downloader.PlaylistEntry, object]] = []

    for entry in entries:
        if entry.video_id in already:
            _LOG.debug("Already downloaded previously: %s", entry.title)
            continue

        if entry.video_id in retired:
            _LOG.debug("Retired as unavailable, not re-checking: %s", entry.title)
            report.deferred += 1
            continue

        if entry.video_id in deferred:
            _LOG.debug("Unavailable last time, next check after %s: %s",
                       deferred[entry.video_id], entry.title)
            report.deferred += 1
            continue

        if cfg.min_duration and entry.duration and entry.duration < cfg.min_duration:
            _LOG.info("Skipping %r - %.0fs is below min_duration_seconds (%ds).",
                      entry.title, entry.duration, cfg.min_duration)
            if record:
                store.record_skip(entry.video_id, title=entry.title, status="too_short",
                                  reason=f"{entry.duration:.0f}s < {cfg.min_duration}s",
                                  matched_path=None, score=0.0, playlist=entry.playlist)
            report.filtered += 1
            continue

        if cfg.max_duration and entry.duration and entry.duration > cfg.max_duration:
            _LOG.info("Skipping %r - %.0fs is above max_duration_seconds (%ds).",
                      entry.title, entry.duration, cfg.max_duration)
            if record:
                store.record_skip(entry.video_id, title=entry.title, status="too_long",
                                  reason=f"{entry.duration:.0f}s > {cfg.max_duration}s",
                                  matched_path=None, score=0.0, playlist=entry.playlist)
            report.filtered += 1
            continue

        match = index.match(
            entry.title, entry.duration, video_id=entry.video_id,
            tolerance=cfg.duration_tolerance, threshold=cfg.title_similarity,
        )

        if match.status == DUPLICATE:
            # At INFO this was ~100 lines every cycle on a library of any
            # size, burying the lines that actually need eyeballing. The
            # "already owned" count in the cycle summary is enough at INFO.
            _LOG.debug("Already in library: %r (%s) -> %s",
                       entry.title, match.reason, match.path)
            if record:
                store.record_skip(entry.video_id, title=entry.title, status="duplicate",
                                  reason=match.reason, matched_path=match.path,
                                  score=match.score, playlist=entry.playlist)
            report.duplicates += 1
            continue

        if match.status == DIFFERENT_VERSION:
            # Close to something owned, but not the same recording - a live cut
            # or a different edit. Downloaded, and called out so it can be
            # eyeballed if the library starts growing near-duplicates.
            #
            # Deliberately not counted or announced at INFO here: this is the
            # decision to *attempt* the download, made before it is known
            # whether the download will even succeed. Counting it here made a
            # track that failed to download still show up as a "near match
            # kept" in the cycle summary. The count and the real announcement
            # happen after the file is actually saved, in _run_downloads.
            _LOG.debug("Will download %r despite a near match (%s) with %s",
                       entry.title, match.reason, match.path)
        elif match.status == AMBIGUOUS:
            _LOG.info("Downloading %r to check it against %s - %s",
                      entry.title, match.path, match.reason)

        plan.append((entry, match))
        if len(plan) >= cfg.max_downloads_per_cycle:
            _LOG.info("Reached max_downloads_per_cycle (%d) - the rest waits for "
                      "the next cycle.", cfg.max_downloads_per_cycle)
            break

    report.planned = len(plan)
    return plan, report


def _progress_logger(cfg: Config, title: str):
    """A callback that logs download progress, but not on every frame.

    yt-dlp reports progress many times a second. Only one line every
    `progress_interval_seconds` is logged, plus the final one, which is what
    makes a three-hour track legible instead of thousands of lines.
    """
    if cfg.progress_interval_seconds <= 0:
        return None
    state = {"last": 0.0}

    def report(text: str) -> None:
        now = time.monotonic()
        done = text.startswith("100") or "100.0%" in text
        if not done and now - state["last"] < cfg.progress_interval_seconds:
            return
        state["last"] = now
        _LOG.info("  %s: %s", title, text)

    return report


def _record_failure(cfg: Config, store: Store, entry, error: str) -> None:
    """Record a failed download and back off before retrying it.

    Every failure now escalates a per-video backoff, not just the ones that
    mean the video itself is gone: an unavailable classification escalates
    through UNAVAILABLE_BACKOFF_HOURS, anything else through the shorter
    TRANSIENT_BACKOFF_HOURS, so a track that keeps failing - for whatever
    reason - stops consuming a download slot and a request every cycle.
    """
    kind = downloader.classify_failure(error)
    permanent = kind == downloader.FAILURE_UNAVAILABLE
    attempts, retry_after = store.record_failure(
        entry.video_id, playlist=entry.playlist, title=entry.title,
        uploader=entry.uploader, duration=entry.duration,
        permanent=permanent, error=error, cap_days=cfg.recheck_unavailable_days,
    )
    if not retry_after:
        _LOG.info("  %r looks unavailable - retired, recheck_unavailable_days is 0.",
                  entry.title)
    elif permanent:
        _LOG.info("  %r looks unavailable (attempt %d) - not checking again until %s.",
                  entry.title, attempts, retry_after)
    else:
        _LOG.info("  %r failed (attempt %d) - not retrying until %s.",
                  entry.title, attempts, retry_after)


def _run_downloads(cfg: Config, store: Store, index: LibraryIndex, plan, report: CycleReport) -> bool:
    """Download everything in `plan`.

    Returns True if YouTube's bot/rate-limit challenge was hit, in which
    case the rest of the plan was deliberately abandoned rather than
    attempted - continuing into a block only makes it worse.
    """
    total = len(plan)
    for position, (entry, pre_match) in enumerate(plan, start=1):
        if _stop.is_set():
            _LOG.info("Stopping before download %d/%d.", position, total)
            return False

        _LOG.info("Downloading %d/%d: %s", position, total, entry.title)
        try:
            result = downloader.download(
                cfg, entry, on_progress=_progress_logger(cfg, entry.title)
            )
        except Exception as err:  # a crashed subprocess must not kill the loop
            _LOG.error("Download of %r raised %s", entry.title, err)
            _record_failure(cfg, store, entry, str(err))
            report.failures += 1
            continue

        if result.skipped_existing:
            _LOG.warning("Not downloaded: %r - %s", entry.title, result.error)
            store.record_skip(entry.video_id, title=entry.title, status="name_collision",
                              reason=result.error, matched_path=None, score=0.0,
                              playlist=entry.playlist)
            report.collisions += 1
            continue

        if not result.ok or not result.path:
            _LOG.error("Download failed: %r - %s", entry.title, result.error)
            # This one genuinely was attempted and failed, so it is recorded
            # as usual - it is everything *after* it that is left untouched.
            _record_failure(cfg, store, entry, result.error or "")
            report.failures += 1
            if downloader.is_bot_block(result.error):
                until = store.register_bot_block()
                _LOG.warning(
                    "YouTube is asking for bot verification - pausing until %s. "
                    "Set cookies_file in the app options to avoid this.", until)
                return True
            continue

        downloader.stamp_video_id(result.path, entry.video_id)
        downloader.split_artist_tags(result.path, result.artists)
        setting = cfg.setting_for(entry.source, entry.playlist)
        if setting is not None:
            downloader.set_genre(result.path, setting.genre)
        duration, file_video_id, variants = read_file_metadata(result.path)

        # Second pass with the real duration. The playlist listing sometimes
        # has no duration at all, and SponsorBlock trimming changes it, so this
        # is the check that actually has the numbers.
        post = index.match(
            entry.title, duration, video_id=entry.video_id,
            tolerance=cfg.duration_tolerance, threshold=cfg.title_similarity,
        )

        if post.status == DUPLICATE:
            action, final_path = _dispose_duplicate(cfg, result.path)
            _LOG.warning("Downloaded %r then found it duplicates %s (%s) - %s",
                         entry.title, post.path, post.reason, action)
            store.record_download(entry.video_id, playlist=entry.playlist, title=entry.title,
                                  uploader=entry.uploader, duration=duration,
                                  path=final_path, status=STATUS_DUPLICATE_AFTER_DOWNLOAD)
            store.record_skip(entry.video_id, title=entry.title,
                              status="duplicate_after_download", reason=post.reason,
                              matched_path=post.path, score=post.score, playlist=entry.playlist)
            report.duplicates_after_download += 1
            if cfg.on_duplicate == "keep" and final_path and variants:
                index.add(LibraryEntry(path=final_path, duration=duration,
                                       video_id=file_video_id or entry.video_id, variants=variants))
        else:
            # The near-match decision was made during planning (_plan_cycle),
            # before it was known whether the download would even succeed.
            # It is only counted and announced now that the file is actually
            # saved, so a track that fails to download never shows up as a
            # "near match kept".
            if pre_match is not None and pre_match.status == DIFFERENT_VERSION:
                report.near_misses += 1
                _LOG.info("Saved %s (kept despite near match with %s: %s)",
                          result.path, pre_match.path, pre_match.reason)
            else:
                _LOG.info("Saved %s", result.path)
            store.record_download(entry.video_id, playlist=entry.playlist, title=entry.title,
                                  uploader=entry.uploader, duration=duration,
                                  path=result.path, status=STATUS_OK)
            report.downloaded += 1
            albumartist.apply_for(result.path, cfg.dest_dir(entry.source, entry.playlist),
                                  frozenset({cfg.quarantine_dir}))
            if variants:
                # Added to the live index so a track appearing on two monitored
                # playlists is not fetched twice in the same cycle.
                index.add(LibraryEntry(path=result.path, duration=duration,
                                       video_id=file_video_id or entry.video_id, variants=variants))

        if cfg.sleep_between_downloads and position < total:
            _stop.wait(cfg.sleep_between_downloads)
    return False


def _publish(cfg: Config, store: Store, publisher: Publisher, status: str,
             report: CycleReport | None = None, note: str = "") -> None:
    if not publisher.enabled:
        return
    stats = store.stats()
    state = {
        "status": status,
        "downloaded": stats["downloaded"],
        "skipped": stats["skipped"],
        "library_files": stats["library_files"],
        "last_run": store.get_meta("last_run") or _utc_now(),
        "problem": "on" if (report.problem if report else bool(note)) else "off",
    }
    attributes = {
        "music_dir": cfg.music_dir,
        "playlists": list(cfg.playlists),
        "dry_run": cfg.dry_run,
        "failed_downloads_total": stats["failed"],
        "skip_breakdown": store.skip_counts(),
        "recent_downloads": [d["title"] for d in store.recent_downloads(10)],
    }
    if report:
        attributes["last_cycle"] = {
            "planned": report.planned,
            "downloaded": report.downloaded,
            "duplicates_skipped": report.duplicates,
            "duplicates_after_download": report.duplicates_after_download,
            "near_misses": report.near_misses,
            "filtered_by_duration": report.filtered,
            "name_collisions": report.collisions,
            "failures": report.failures,
            "playlist_errors": report.playlist_errors,
        }
        if report.would_download:
            attributes["dry_run_would_download"] = report.would_download
    if note:
        attributes["note"] = note
    publisher.publish_state(state, attributes)


def run_cycle(cfg: Config, store: Store, publisher: Publisher) -> CycleReport:
    report = CycleReport()

    if not os.path.isdir(cfg.music_dir):
        _LOG.error("music_dir %s is not there.", cfg.music_dir)
        _LOG.error("If that is a Home Assistant network storage mount, check it "
                   "under Settings > System > Storage - the add-on sees media "
                   "mounts as /media/<mount name> and share mounts as "
                   "/share/<mount name>. Nothing will be downloaded until the "
                   "path exists.")
        report.playlist_errors += 1
        _publish(cfg, store, publisher, "error", report, note=f"{cfg.music_dir} not available")
        return report

    _publish(cfg, store, publisher, "scanning", None)
    index, scan = build_index(cfg, store)
    _LOG.info("Library indexed: %d audio files (%d read fresh, %d stale entries "
              "dropped) in %.1fs.", scan.files, scan.reread, scan.pruned, scan.seconds)
    if scan.files == 0:
        _LOG.warning("No audio files found under %s - every playlist entry will "
                     "look new. Check music_dir points at the right folder.", cfg.music_dir)

    entries: list[downloader.PlaylistEntry] = []
    seen_ids: set[str] = set()
    for url in cfg.playlists:
        if _stop.is_set():
            return report
        _LOG.info("Reading playlist %s", url)
        try:
            title, found = downloader.fetch_playlist(cfg, url)
        except Exception as err:
            _LOG.error("Could not read playlist %s: %s", url, err)
            report.playlist_errors += 1
            continue
        _LOG.info("Playlist %r: %d available entries.", title, len(found))
        for entry in found:
            entry = replace(entry, source=url)
            # The same track on two monitored playlists is one download.
            if entry.video_id not in seen_ids:
                seen_ids.add(entry.video_id)
                entries.append(entry)

    _publish(cfg, store, publisher, "comparing", None)
    plan, plan_report = _plan_cycle(cfg, store, index, entries, record=not cfg.dry_run)
    plan_report.playlist_errors = report.playlist_errors
    report = plan_report

    if cfg.dry_run:
        report.would_download = [entry.title for entry, _ in plan]
        _LOG.warning("DRY RUN - nothing was downloaded. %d track(s) would be "
                     "fetched, %d already in the library, %d filtered by duration.",
                     len(plan), report.duplicates, report.filtered)
        for title in report.would_download:
            _LOG.warning("  would download: %s", title)
        _LOG.warning("Set dry_run to false in the add-on configuration to let it "
                     "write to %s.", cfg.music_dir)
    elif plan:
        _publish(cfg, store, publisher, "downloading", report)
        bot_blocked = _run_downloads(cfg, store, index, plan, report)
        if not bot_blocked:
            # A clean run through the plan - any earlier block has cleared,
            # so the next one (if there is one) starts the escalation over
            # rather than doubling from wherever an old one left off.
            store.clear_bot_backoff()
    else:
        _LOG.info("Nothing new to download.")
        store.clear_bot_backoff()

    store.set_meta("last_run", _utc_now())
    _LOG.info("Cycle done: %d downloaded, %d already owned, %d near matches kept, "
              "%d duplicates caught after download, %d failed, "
              "%d deferred (backing off).",
              report.downloaded, report.duplicates, report.near_misses,
              report.duplicates_after_download, report.failures, report.deferred)
    _publish(cfg, store, publisher, "idle", report)
    return report


def main() -> int:
    # Logging is set up before the config is loaded - settings.load() itself
    # logs (e.g. a duplicate playlist being dropped), and a log call with no
    # handler configured yet is silently swallowed rather than queued. The
    # level is corrected to what the user actually asked for immediately
    # after, so only this one bootstrap message is ever affected.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    cfg = settings.load()
    logging.getLogger().setLevel(_LEVELS.get(cfg.log_level, logging.INFO))
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    _LOG.info("YouTube Music Sync starting.")
    # Every setting that has ever been reported as "not sticking" is named
    # here. A configuration that silently reverted used to look identical in
    # the log to one that was applied; now the two can be told apart.
    _LOG.info("Library: %s | playlists: %d | format: %s | every %d min | "
              "dry_run: %s | aria2c: %s | max/cycle: %d",
              cfg.music_dir, len(cfg.playlists), cfg.audio_format,
              cfg.poll_interval_minutes, cfg.dry_run, cfg.use_aria2c,
              cfg.max_downloads_per_cycle)
    for url in cfg.playlists:
        _LOG.info("  playlist: %s", url)

    problems = cfg.problems()
    publisher = Publisher(cfg)
    publisher.connect()
    publisher.publish_discovery()

    if problems:
        for problem in problems:
            _LOG.error("%s", problem)
        _LOG.error("Fix the add-on configuration and restart. Idling until then.")
        if publisher.enabled:
            publisher.publish_state(
                {"status": "misconfigured", "downloaded": 0, "skipped": 0,
                 "library_files": 0, "last_run": _utc_now(), "problem": "on"},
                {"problems": problems},
            )
        while not _stop.is_set():
            _stop.wait(3600)
        publisher.close()
        return 1

    os.makedirs(cfg.tmp_dir, exist_ok=True)
    # Nothing is running at boot, so anything left in here is debris from an
    # interrupted download on a previous start - most importantly a stale
    # .part/.aria2 file that would otherwise make the next attempt to resume
    # it fail with HTTP 416 forever.
    removed = downloader.clear_tmp_dir(cfg.tmp_dir)
    if removed:
        _LOG.info("Cleared %d leftover temp file(s) from a previous run.", removed)

    store = Store(cfg.db_path)

    try:
        while not _stop.is_set():
            started = time.monotonic()
            bot_until = store.bot_backoff_until()
            if bot_until:
                _LOG.info("Still pausing for YouTube's bot-check backoff until %s "
                          "- skipping this cycle.", bot_until)
            else:
                try:
                    run_cycle(cfg, store, publisher)
                except Exception as err:
                    _LOG.exception("Cycle failed: %s", err)
                    _publish(cfg, store, publisher, "error", None, note=str(err))

            if _stop.is_set():
                break
            elapsed = time.monotonic() - started
            # A bot-block hit during the cycle just above, or one already in
            # effect from before this start, both land here: the next cycle
            # is scheduled for when the backoff actually clears rather than
            # the normal poll interval, which would otherwise just waste
            # that cycle re-discovering the same block.
            bot_until = store.bot_backoff_until()
            if bot_until:
                remaining = (datetime.fromisoformat(bot_until) - datetime.now(timezone.utc)).total_seconds()
                wait = max(60.0, remaining)
            else:
                wait = max(60.0, cfg.poll_interval_minutes * 60 - elapsed)
            _LOG.info("Next check in %.0f minutes.", wait / 60)
            _stop.wait(wait)
    finally:
        _LOG.info("Shutting down.")
        publisher.close()
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
