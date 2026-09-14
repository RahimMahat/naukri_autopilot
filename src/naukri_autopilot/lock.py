"""Single-instance lock.

A scheduled tick and a dashboard "Run now" can land at the same moment, and two
browsers driving one Naukri session at once is a good way to produce a confusing
half-failure. An OS-level file lock is used rather than a PID file because the
kernel releases it when the process dies - no stale-lock heuristics, no guessing
whether PID 4312 is still our tick or someone else's Notepad.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

from . import config

try:  # Windows
    import msvcrt

    _WINDOWS = True
except ImportError:  # POSIX
    import fcntl

    _WINDOWS = False


def _try_lock(handle) -> bool:
    try:
        if _WINDOWS:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(handle) -> None:
    try:
        if _WINDOWS:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


@contextlib.contextmanager
def exclusive(path: "Path | None" = None):
    """Yields True if this process holds the lock, False if another does.

    Never blocks: a tick that cannot get the lock records SKIPPED_LOCKED and
    exits, because the run it would duplicate is already happening.
    """
    config.ensure_dirs()
    lock_path = Path(path or config.LOCK_PATH)
    handle = open(str(lock_path), "a+b")
    acquired = False
    try:
        handle.seek(0)
        acquired = _try_lock(handle)
        if acquired:
            try:
                handle.truncate(0)
                handle.write(str(os.getpid()).encode("ascii"))
                handle.flush()
                handle.seek(0)
            except OSError:
                pass
        yield acquired
    finally:
        if acquired:
            _unlock(handle)
        handle.close()
