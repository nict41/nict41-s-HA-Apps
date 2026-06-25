"""Live progress for the sync triggered by "Check mail now", spanning both of
run_sync_cycle's slow phases - the IMAP mail scan, then the tracking-provider
refresh - polled by the dashboard's /sync/status endpoint.

Without a second stage here, the dashboard's "checked X/Y" count would stop
moving the moment the mail scan finishes and just sit frozen for however
long the provider refresh (register + query against Track123/17Track,
covering every parcel due for a check) takes afterward, even though the
underlying request is still very much in progress - which looks
indistinguishable from no progress being made at all. A shared module (not
owned by mail_worker) since both mail_worker's folder scan and main.py's
provider-refresh loop need to report into the same state.
"""

import threading

_lock = threading.Lock()
_state = {"running": False, "stage": None, "checked": 0, "total": 0}


def get() -> dict:
    with _lock:
        return dict(_state)


def start_stage(stage: str) -> None:
    with _lock:
        _state.update(running=True, stage=stage, checked=0, total=0)


def add_total(count: int) -> None:
    with _lock:
        _state["total"] += count


def increment(count: int = 1) -> None:
    with _lock:
        _state["checked"] += count


def finish() -> None:
    with _lock:
        _state.update(running=False, stage=None)
