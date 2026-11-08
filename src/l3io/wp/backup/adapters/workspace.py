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

:func:`sweep_stale` (BL-E002-065, BL-E003-086; owner decision 2026-11-07)
removes the plaintext residue a hard-killed Run leaves: this OS user's
``l3io-wp-backup-*`` workspaces and staged ``.partial`` Archives whose newest
modification is older than :data:`STALE_AFTER_SECONDS`. A live Run keeps
writing into its workspace or staged file, so the age cutoff never reaches
it; each Run also holds its Site Lock while it sweeps. Each removal is logged
by path (``stale_residue_removed``), the owner's explicit ask.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

import structlog

from l3io.wp.backup.domain.errors import ArchiveWriteError
from l3io.wp.backup.domain.storage_target import DEFAULT_ARCHIVE_NAME_GLOB

__all__ = [
    "PRIVATE_DIR_MODE",
    "STAGED_SUFFIX",
    "STALE_AFTER_SECONDS",
    "WORKSPACE_PREFIX",
    "private_directories",
    "run_workspace",
    "sweep_stale",
    "sweep_stale_partials",
    "sweep_stale_workspaces",
]

WORKSPACE_PREFIX = "l3io-wp-backup-"
#: The mode of every directory :func:`private_directories` creates.
PRIVATE_DIR_MODE = 0o700
#: Residue untouched for this long is a dead Run's (owner decision: 24 h).
STALE_AFTER_SECONDS = 24 * 60 * 60

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


def _newest_mtime(path: Path) -> float:
    """The latest ``st_mtime`` of ``path`` and, for a directory, all under it."""
    newest = path.lstat().st_mtime
    if path.is_dir() and not path.is_symlink():
        for root, dirs, files in os.walk(path):
            for name in (*dirs, *files):
                with contextlib.suppress(OSError):
                    newest = max(newest, Path(root, name).lstat().st_mtime)
    return newest


def sweep_stale(candidates: Iterable[Path], *, now: float) -> tuple[Path, ...]:
    """Remove each candidate this user owns that is older than the cutoff.

    A symlink is never followed or removed, nor is anything another user
    owns. ``now`` is a POSIX timestamp. Failures are skipped: the sweep is
    housekeeping, never a reason for the Run to fail. Returns what it removed.
    """
    removed: list[Path] = []
    uid = os.getuid() if hasattr(os, "getuid") else None
    for path in candidates:
        try:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or (uid is not None and info.st_uid != uid):
                continue
            if now - _newest_mtime(path) < STALE_AFTER_SECONDS:
                continue
            if stat.S_ISDIR(info.st_mode):
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError:
            continue
        _log.info("stale_residue_removed", path=str(path))
        removed.append(path)
    return tuple(removed)


def sweep_stale_workspaces(parent: Path | None, *, now: float) -> tuple[Path, ...]:
    """:func:`sweep_stale` over the ``l3io-wp-backup-*`` workspaces in ``parent``.

    ``parent=None`` is where :func:`run_workspace` creates them:
    ``tempfile.gettempdir()`` (``$TMPDIR``).
    """
    where = Path(tempfile.gettempdir()) if parent is None else parent
    try:
        candidates = sorted(where.glob(f"{WORKSPACE_PREFIX}*"))
    except OSError:
        return ()
    return sweep_stale(candidates, now=now)


#: The suffix ``adapters/store.py`` gives a staged Archive.
STAGED_SUFFIX = ".partial"


def sweep_stale_partials(directory: Path, name: str, *, now: float) -> tuple[Path, ...]:
    """:func:`sweep_stale` over ``directory``'s staged files of Archive ``name``.

    Those of ``name`` itself and of any default-named Archive
    (``.<name>.<random>.partial``, the store's staging spelling).
    """
    globs = {
        f".{name}.*{STAGED_SUFFIX}",
        f".{DEFAULT_ARCHIVE_NAME_GLOB}.*{STAGED_SUFFIX}",
    }
    try:
        candidates = sorted({p for g in globs for p in directory.glob(g)})
    except OSError:
        return ()
    return sweep_stale(candidates, now=now)
