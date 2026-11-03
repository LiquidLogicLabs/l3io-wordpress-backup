"""AD-19 — hold a Site's lock from another process, for the Site Lock tests.

:func:`held_elsewhere` starts a ``multiprocessing`` **spawn** child (never
``fork``, which would inherit this process's descriptors and lock registry)
that acquires the Site Lock through ``FcntlSiteLock`` and holds it until the
block ends. Used by ``tests/adapters/test_site_lock.py`` and
``tests/integration/test_exit_codes_end_to_end.py``.
"""

from __future__ import annotations

import multiprocessing
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from multiprocessing.synchronize import Event
from pathlib import Path

__all__ = ["HOLDER_HOST", "HOLDER_RUN_ID", "Holder", "held_elsewhere"]

HOLDER_RUN_ID = "11111111-2222-4333-8444-555555555555"
HOLDER_HOST = "holder.example"
_TIMEOUT = 30.0


@dataclass(frozen=True)
class Holder:
    """The child holding the lock."""

    pid: int
    run_id: str


def _hold(
    site_root: str,
    lock_dir: str | None,
    state_home: str | None,
    ready: Event,
    done: Event,
) -> None:
    if state_home is not None:
        os.environ["XDG_STATE_HOME"] = state_home
    from l3io.wp.backup.adapters.lock import FcntlSiteLock
    from l3io.wp.backup.domain.lock import LockHolder

    lock = FcntlSiteLock(None if lock_dir is None else Path(lock_dir))
    holder = LockHolder(
        run_id=HOLDER_RUN_ID,
        pid=os.getpid(),
        started_at=datetime(2026, 11, 3, 12, 0, tzinfo=UTC),
        hostname=HOLDER_HOST,
    )
    with lock.acquire(Path(site_root), holder):
        ready.set()
        done.wait(_TIMEOUT)


@contextmanager
def held_elsewhere(site_root: Path, lock_dir: Path | None = None) -> Iterator[Holder]:
    """``site_root``'s lock, held by a spawned child for the block's duration."""
    context = multiprocessing.get_context("spawn")
    ready, done = context.Event(), context.Event()
    child = context.Process(
        target=_hold,
        args=(
            str(site_root),
            None if lock_dir is None else str(lock_dir),
            os.environ.get("XDG_STATE_HOME"),
            ready,
            done,
        ),
    )
    child.start()
    try:
        assert ready.wait(_TIMEOUT), "the holder never acquired the lock"
        assert child.pid is not None
        yield Holder(pid=child.pid, run_id=HOLDER_RUN_ID)
    finally:
        done.set()
        child.join(_TIMEOUT)
        if child.is_alive():
            child.kill()
            child.join()
