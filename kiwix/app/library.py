"""The local ZIM library: what's on the share, and what kiwix-serve serves.

Two separate things live here on purpose:

* the *files* - whatever `.zim` archives are sitting in the storage
  directory, plus any half-finished `.part` downloads next to them;
* the *selection* - which of those files kiwix-serve is currently serving,
  persisted in the add-on's own /data so it survives restarts and never
  writes bookkeeping onto the NAS share.

The served set is materialised as a kiwix library XML file built with
`kiwix-manage`; kiwix-serve watches that file (`--monitorLibrary`) and picks
up changes without a restart.
"""

import json
import subprocess
import threading
from pathlib import Path

import settings

PART_SUFFIX = ".part"

_STATE_LOCK = threading.Lock()

_DEFAULT_STATE = {"served": [], "server_enabled": True}


def state_file() -> Path:
    return settings.DATA_DIR / "state.json"


def library_xml() -> Path:
    return settings.DATA_DIR / "library.xml"


def read_state() -> dict:
    try:
        raw = json.loads(state_file().read_text())
    except (OSError, ValueError):
        return dict(_DEFAULT_STATE)
    state = dict(_DEFAULT_STATE)
    if isinstance(raw.get("served"), list):
        state["served"] = [str(name) for name in raw["served"]]
    if isinstance(raw.get("server_enabled"), bool):
        state["server_enabled"] = raw["server_enabled"]
    return state


def write_state(state: dict) -> None:
    with _STATE_LOCK:
        settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = state_file().with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(state_file())


def _human_entry(path: Path, served: set[str]) -> dict:
    stat = path.stat()
    return {
        "filename": path.name,
        "title": path.stem.replace("_", " "),
        "size": stat.st_size,
        "modified": stat.st_mtime,
        "served": path.name in served,
        "complete": True,
    }


def list_zims() -> list[dict]:
    """Every archive in the storage directory, complete and partial.

    Returns an empty list rather than raising when the share is missing, so
    callers can render the storage error on its own.
    """
    status = settings.storage_status()
    if not status["ok"]:
        return []

    directory = Path(status["path"])
    served = set(read_state()["served"])
    entries: list[dict] = []

    try:
        children = sorted(directory.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return []

    for child in children:
        try:
            if child.is_file() and child.suffix == ".zim":
                entries.append(_human_entry(child, served))
            elif child.is_file() and child.name.endswith(".zim" + PART_SUFFIX):
                stat = child.stat()
                entries.append(
                    {
                        "filename": child.name[: -len(PART_SUFFIX)],
                        "title": child.name[: -len(".zim" + PART_SUFFIX)].replace("_", " "),
                        "size": stat.st_size,
                        "modified": stat.st_mtime,
                        "served": False,
                        "complete": False,
                    }
                )
        except OSError:
            # A file that vanished mid-scan (or a share that dropped between
            # iterdir and stat) shouldn't take the whole listing down.
            continue

    return entries


def downloaded_filenames() -> set[str]:
    return {entry["filename"] for entry in list_zims() if entry["complete"]}


def zim_path_for(filename: str) -> Path | None:
    """The full path of a ZIM in the storage directory.

    Returns None for anything that isn't a plain `.zim` filename directly in
    that directory - the filename reaches here from an HTTP request, so path
    traversal has to be impossible.
    """
    if not filename.endswith(".zim") or "/" in filename or "\\" in filename:
        return None
    if filename != Path(filename).name or filename.startswith("."):
        return None
    status = settings.storage_status()
    if not status["ok"]:
        return None
    return Path(status["path"]) / filename


def set_served(filename: str, served: bool) -> dict:
    state = read_state()
    current = [name for name in state["served"] if name != filename]
    if served:
        current.append(filename)
    state["served"] = sorted(current)
    write_state(state)
    rebuild_library_xml()
    return state


def prune_served() -> None:
    """Drop selections whose file is gone (deleted, or share swapped out)."""
    status = settings.storage_status()
    if not status["ok"]:
        # The share being away is not evidence that a file was removed - keep
        # the selection so it comes back with the share.
        return
    present = downloaded_filenames()
    state = read_state()
    kept = [name for name in state["served"] if name in present]
    if kept != state["served"]:
        state["served"] = kept
        write_state(state)


def served_paths() -> list[Path]:
    status = settings.storage_status()
    if not status["ok"]:
        return []
    directory = Path(status["path"])
    return [directory / name for name in read_state()["served"] if (directory / name).is_file()]


def rebuild_library_xml() -> list[str]:
    """Write the kiwix library XML for the current selection.

    kiwix-serve is watching this file, so it reloads on its own. Returns the
    list of files kiwix-manage refused (an invalid or truncated ZIM), which
    are reported in the UI rather than being allowed to break the server.
    """
    settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
    target = library_xml()
    tmp = target.with_suffix(".xml.tmp")
    tmp.unlink(missing_ok=True)

    rejected: list[str] = []
    for path in served_paths():
        try:
            result = subprocess.run(
                ["kiwix-manage", str(tmp), "add", str(path)],
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            # No kiwix-manage on PATH (it ships in the add-on image, but the
            # test suite runs the app outside it) - nothing to add, and
            # certainly nothing worth crashing over.
            print(f"[kiwix] could not run kiwix-manage: {exc}")
            break
        if result.returncode != 0:
            rejected.append(path.name)
            print(f"[kiwix] kiwix-manage rejected {path.name}: {result.stderr.strip()}")

    if not tmp.exists():
        # kiwix-manage only creates the file when it adds something; an empty
        # selection still needs a valid (empty) library for kiwix-serve.
        tmp.write_text('<?xml version="1.0" encoding="UTF-8" ?>\n<library version="20110515">\n</library>\n')

    tmp.replace(target)
    return rejected


def delete_zim(filename: str) -> tuple[bool, str]:
    """Delete an archive and any partial download of it. Never raises."""
    path = zim_path_for(filename)
    if path is None:
        return False, "Invalid filename, or the storage share is unavailable."

    removed = False
    for candidate in (path, path.with_name(path.name + PART_SUFFIX)):
        try:
            if candidate.exists():
                candidate.unlink()
                removed = True
        except OSError as exc:
            return False, f"Could not delete {candidate.name}: {exc}"

    if not removed:
        return False, f"{filename} is not in the library."

    set_served(filename, False)
    return True, ""
