"""BL-E007-009, BL-E007-019 — how one ``wp`` child process is run.

:func:`run_child` runs an argument vector with exactly the environment it is
given, no shell and no stdin, through ``adapters/preflight.py::run_bounded``:
the child leads its own process group, killed whole after
:data:`CHILD_TIMEOUT_S` or once either output stream passes
:data:`CHILD_OUTPUT_LIMIT`. While a child runs this process is non-dumpable
(:func:`_unreadable_by_the_child`), so the restored Site's PHP cannot read
its ``/proc/<ppid>/environ``. ``adapters/wpcli.py`` builds the argument
vectors and reads the output; this module only runs the process.
"""

from __future__ import annotations

import contextlib
import ctypes
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Final, Protocol

from l3io.wp.backup.adapters.preflight import (
    run_bounded,
)

__all__ = ["CHILD_OUTPUT_LIMIT", "CHILD_TIMEOUT_S", "ChildRunner", "run_child"]


class ChildRunner(Protocol):
    """What runs one ``wp`` child; injectable so tests can record the call."""

    def __call__(
        self, argv: list[str], *, cwd: Path, env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]: ...


#: The most one ``wp`` child may print on each of stdout and stderr, in bytes
#: (BL-E007-009); past it the child's process group is killed and the call
#: fails. A ``search-replace`` table of every column of a large Site fits.
CHILD_OUTPUT_LIMIT: Final = 8 * 1024 * 1024
#: The longest one ``wp`` child may run, in seconds. Generous: a ``--precise``
#: replace unserializes every row. It exists so restored code that never returns
#: cannot hold the Site Lock for ever.
CHILD_TIMEOUT_S: Final = 4 * 60 * 60
#: ``prctl(2)``'s ``PR_SET_DUMPABLE`` and ``PR_GET_DUMPABLE`` option numbers.
_PR_SET_DUMPABLE: Final = 4
_PR_GET_DUMPABLE: Final = 3


class _DumpableGuard:
    """Reference-counts the process-wide ``PR_SET_DUMPABLE`` toggle.

    The flag belongs to the process, not to a thread, so two overlapping
    ``wp`` children (a library host running two restores) must share one
    toggle: it drops on the first holder and is put back only when the last
    one is done. Without the count the first thread to finish re-enabled
    ``/proc`` access while the other's child was still running.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._holders = 0
        self._previous = 0

    def enter(self) -> None:
        with self._lock:
            if self._holders == 0:
                self._previous = _set_dumpable(0)
            self._holders += 1

    def leave(self) -> None:
        with self._lock:
            self._holders -= 1
            if self._holders == 0 and self._previous:
                _set_dumpable(1)  # PR_SET_DUMPABLE takes 0 or 1 only


_DUMPABLE = _DumpableGuard()


@contextlib.contextmanager
def _unreadable_by_the_child() -> Iterator[None]:
    """Make this process's ``/proc/<pid>`` entries unreadable to its own children.

    The child runs the restored Site's PHP as the same user, and a same-user
    process can read ``/proc/<ppid>/environ``: the passphrase and the admin
    credentials this process was started with. A non-dumpable process's entries
    belong to root. The flag is reset by ``execve``, so the child itself is not
    affected, and it is put back once the last overlapping child is done
    (:class:`_DumpableGuard`). Linux only; elsewhere, or if the call fails, it
    does nothing (``docs/restore.md`` says so). While a child runs the whole
    host process is non-dumpable: no core dump, no same-user ``ptrace``.
    """
    _DUMPABLE.enter()
    try:
        yield
    finally:
        _DUMPABLE.leave()


def _set_dumpable(value: int) -> int:
    """Set ``PR_SET_DUMPABLE`` and return the previous value; ``0`` if unsupported."""
    if not sys.platform.startswith("linux"):
        return 0
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        previous = int(libc.prctl(_PR_GET_DUMPABLE, 0, 0, 0, 0))
        if previous < 0 or libc.prctl(_PR_SET_DUMPABLE, value, 0, 0, 0) != 0:
            return 0
    except (OSError, AttributeError):
        return 0
    return previous


def run_child(
    argv: list[str], *, cwd: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` with exactly ``env``, no shell, no stdin, text output.

    ``adapters/preflight.py::run_bounded``: the child leads its own process
    group, killed whole after :data:`CHILD_TIMEOUT_S`
    (``subprocess.TimeoutExpired``) or once either stream passes
    :data:`CHILD_OUTPUT_LIMIT` (``ChildOutputLimitError``).
    Runs with this process non-dumpable, so the child cannot read its ``/proc``.
    """
    with _unreadable_by_the_child():
        return run_bounded(
            argv,
            cwd=cwd,
            env=env,
            timeout=CHILD_TIMEOUT_S,
            limit=CHILD_OUTPUT_LIMIT,
        )
