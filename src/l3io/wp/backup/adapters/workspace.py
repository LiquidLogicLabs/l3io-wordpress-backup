"""AD-12 — every Run owns a private workspace.

:func:`run_workspace` creates the directory with stdlib
:func:`tempfile.mkdtemp` (mode ``0700`` by construction), yields it, and
removes it with :func:`shutil.rmtree` on success **and** on failure. ``parent``
overrides only *where* it is created. The Dump lives only in here, under a
per-Run directory — never at a fixed shared path. E002-S01-004 moves the
workspace under the Run Context.

:func:`private_directories` is the one way this package creates a directory
tree that may not exist yet (the per-user state directory, a Safety Snapshot
directory): every component it creates is ``0700``, as the XDG Base Directory
specification asks of a missing state directory. ``Path.mkdir(parents=True)``
would create the ancestors with the umask's mode (BL-E003-077);
``tests/contracts/test_private_directories.py`` refuses it anywhere in the
package.
"""

from __future__ import annotations

import contextlib
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import structlog

from l3io.wp.backup.domain.errors import ArchiveWriteError

__all__ = [
    "PRIVATE_DIR_MODE",
    "WORKSPACE_PREFIX",
    "private_directories",
    "run_workspace",
]

WORKSPACE_PREFIX = "l3io-wp-backup-"
#: The mode of every directory :func:`private_directories` creates.
PRIVATE_DIR_MODE = 0o700

_log = structlog.get_logger(__name__)


@contextmanager
def run_workspace(parent: Path | None) -> Iterator[Path]:
    """A private ``0700`` directory for one Run, removed when the block exits."""
    try:
        created = Path(tempfile.mkdtemp(prefix=WORKSPACE_PREFIX, dir=parent))
    except OSError as exc:
        where = parent if parent is not None else Path(tempfile.gettempdir())
        raise ArchiveWriteError(where, exc.strerror or type(exc).__name__) from exc
    _log.debug("workspace_created", path=str(created))
    try:
        yield created
    finally:
        shutil.rmtree(created, ignore_errors=True)
        if created.exists():
            # The path only at DEBUG (AD-12); the warning names no path.
            _log.warning("workspace_not_removed")
            _log.debug("workspace_not_removed_path", path=str(created))


def private_directories(path: Path) -> None:
    """Create ``path`` and each missing ancestor, every one at mode ``0700``.

    Existing directories are left as they are; the caller tightens the leaf
    when it must. ``OSError`` propagates (an ancestor that is a file, no
    permission), so each caller maps it to its own error.
    """
    missing: list[Path] = []
    for candidate in (path, *path.parents):
        if candidate.is_dir():
            break
        missing.append(candidate)
    for directory in reversed(missing):
        # Another process may create it first; that is not a failure.
        with contextlib.suppress(FileExistsError):
            directory.mkdir(mode=PRIVATE_DIR_MODE)
    if not path.is_dir():
        raise NotADirectoryError(str(path))
