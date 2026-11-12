"""Save history for the screens file: a snapshot before every write.

Every time the editor saves, the previous content of the screens file is copied
into <screens file folder>/.history/screens-YYYYMMDD-HHMMSS-ffffff.yaml, so any
save (including a restore) can be undone. Only the newest `keep` snapshots are
kept, and a snapshot identical to the newest one is skipped.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from typing import Callable

_LOG = logging.getLogger(__name__)

_ID_RE = re.compile(r"^screens-\d{8}-\d{6}-\d{6}$")


class SaveHistory:
    """Snapshots of the screens file in <dir of screens_file>/.history."""

    def __init__(self, screens_file: str, keep: int = 30,
                 now: Callable[[], datetime] = datetime.now) -> None:
        self._screens_file = screens_file
        self._folder = os.path.join(os.path.dirname(screens_file) or ".", ".history")
        self._keep = keep
        self._now = now

    def snapshot(self, text: str) -> str | None:
        """Write `text` as a new snapshot; returns its id, or None if it equals
        the newest snapshot (nothing changed)."""
        items = self.list()
        if items:
            try:
                if self.read(items[0]["id"]) == text:
                    return None
            except (KeyError, OSError):
                pass
        stamp = self._now()
        name = stamp.strftime("screens-%Y%m%d-%H%M%S-%f")
        os.makedirs(self._folder, exist_ok=True)
        path = os.path.join(self._folder, name + ".yaml")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
        self._prune()
        return name

    def list(self) -> list[dict]:
        """All snapshots, newest first: {"id", "saved_at", "size"}."""
        out = []
        try:
            names = os.listdir(self._folder)
        except OSError:
            return out
        for name in names:
            if not name.endswith(".yaml") or not _ID_RE.match(name[:-5]):
                continue
            path = os.path.join(self._folder, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            saved_at = datetime.fromtimestamp(st.st_mtime).isoformat()
            out.append({"id": name[:-5], "saved_at": saved_at, "size": st.st_size})
        # ids start with the timestamp, so sorting by id sorts by time
        out.sort(key=lambda item: item["id"], reverse=True)
        return out

    def read(self, id: str) -> str:
        """The snapshot's text. KeyError for a bad or unknown id."""
        if not isinstance(id, str) or not _ID_RE.match(id):
            raise KeyError(id)
        path = os.path.join(self._folder, id + ".yaml")
        if not os.path.isfile(path):
            raise KeyError(id)
        with open(path, encoding="utf-8") as f:
            return f.read()

    def _prune(self) -> None:
        """Delete all but the newest `keep` snapshots."""
        items = self.list()
        for item in items[self._keep:]:
            try:
                os.remove(os.path.join(self._folder, item["id"] + ".yaml"))
            except OSError:
                pass
