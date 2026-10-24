"""Building the library index, re-reading only what changed since last time."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

from library import (
    LibraryEntry,
    LibraryIndex,
    iter_audio_files,
    read_file_metadata,
    variants_from_json,
    variants_to_json,
)
from settings import Config
from state import Store

_LOG = logging.getLogger(__name__)

_BATCH = 200


@dataclass
class ScanStats:
    files: int = 0
    reread: int = 0
    pruned: int = 0
    seconds: float = 0.0


def build_index(cfg: Config, store: Store) -> tuple[LibraryIndex, ScanStats]:
    """Index every audio file under music_dir and any playlist folders, using the cache where valid."""
    started = time.monotonic()
    cached = store.load_library_cache()
    stats = ScanStats()
    index = LibraryIndex()
    pending: list[tuple] = []
    seen: set[str] = set()

    # The quarantine folder holds files already judged duplicates; indexing
    # them would make every future copy look like a duplicate of a duplicate.
    skip_dirs = frozenset({os.path.normpath(cfg.quarantine_dir)})

    paths = (p for root in cfg.library_dirs if os.path.isdir(root)
             for p in iter_audio_files(root, skip_dirs=skip_dirs))
    for path in paths:
        try:
            stat = os.stat(path)
        except OSError as err:
            _LOG.debug("Could not stat %s: %s", path, err)
            continue

        seen.add(path)
        stats.files += 1
        hit = cached.get(path)

        if hit and hit.size == stat.st_size and hit.mtime_ns == stat.st_mtime_ns:
            variants = variants_from_json(hit.variants)
            duration, video_id = hit.duration, hit.video_id
        else:
            duration, video_id, variants = read_file_metadata(path)
            stats.reread += 1
            pending.append(
                (path, stat.st_size, stat.st_mtime_ns, duration, video_id,
                 variants_to_json(variants))
            )
            if len(pending) >= _BATCH:
                store.save_library_files(pending)
                pending = []

        if variants:
            index.add(LibraryEntry(path=path, duration=duration, video_id=video_id, variants=variants))

    store.save_library_files(pending)
    stats.pruned = store.prune_library_cache(seen)
    stats.seconds = time.monotonic() - started
    return index, stats
