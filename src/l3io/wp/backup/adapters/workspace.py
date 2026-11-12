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
import errno
import os
import shutil
import stat
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

import structlog

from l3io.wp.backup.domain.errors import ArchiveWriteError, BackupError
from l3io.wp.backup.domain.storage_target import DEFAULT_ARCHIVE_NAME_GLOBS

__all__ = [
    "PRIVATE_DIR_MODE",
    "STAGED_SUFFIX",
    "STALE_AFTER_SECONDS",
    "STATE_HOME_HINT",
    "WORKSPACE_PREFIX",
    "WorkspaceNotRemovedError",
    "private_directories",
    "private_state_directories",
    "require_private_state_directory",
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


class WorkspaceNotRemovedError(BackupError):
    """The Run's workspace survived its removal, and may hold a plaintext Dump.

    Raised only when the Run had otherwise succeeded (BL-E002-041), so it
    does not exit ``OK`` with a copy of the database left behind; a failing
    Run keeps its own error and the WARNING. ``path`` is the workspace; the
    message names only its prefix, since the path is logged at ``DEBUG`` only.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(
            "the Run's private workspace could not be removed and may hold a "
            f"plaintext database dump; remove the {WORKSPACE_PREFIX}* directory "
            "under the temporary directory (a later Run removes it once it is "
            "24 hours old)"
        )


@contextmanager
def run_workspace(parent: Path | None) -> Iterator[Path]:
    """A private ``0700`` directory for one Run, removed when the block exits.

    A workspace that cannot be removed is a WARNING; on a block that exited
    normally it is also :class:`WorkspaceNotRemovedError`.
    """
    try:
        created = Path(tempfile.mkdtemp(prefix=WORKSPACE_PREFIX, dir=parent))
    except OSError as exc:
        where = parent if parent is not None else Path(tempfile.gettempdir())
        raise ArchiveWriteError(where, exc.strerror or type(exc).__name__) from exc
    _log.debug("workspace_created", path=str(created))
    try:
        yield created
    except BaseException:
        _remove(created)
        raise
    if not _remove(created):
        raise WorkspaceNotRemovedError(created)


def _remove(created: Path) -> bool:
    """Remove the workspace; ``False``, with a WARNING, when it survives."""
    shutil.rmtree(created, ignore_errors=True)
    if not created.exists():
        return True
    # The path only at DEBUG (AD-12); the warning names no path.
    _log.warning("workspace_not_removed")
    _log.debug("workspace_not_removed_path", path=str(created))
    return False


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


def _effective_uid() -> int | None:
    """``os.geteuid()``, the owner of every file this process creates; ``None``
    off POSIX.

    Ownership is judged against the effective uid, not the real one (owner
    decision 44, 2026-11-07): under ``sudo -E`` the real and effective users
    differ, and what matters is who will own the files the Run writes.
    """
    geteuid = getattr(os, "geteuid", None)
    return None if geteuid is None else int(geteuid())


#: The remedy a state-directory refusal names (owner decision 44).
STATE_HOME_HINT: Final = (
    "point XDG_STATE_HOME at a directory this user owns "
    "(sudo -E keeps the invoking user's environment)"
)


def private_state_directories(path: Path, *, root: Path) -> None:
    """:func:`private_directories` for a state directory, checked before and after.

    The components that already exist are checked first
    (:func:`require_private_state_directory`), so nothing is created through a
    symlink or in another user's directory; the whole tree is checked again
    once created.
    """
    require_private_state_directory(path, root=root, missing_ok=True)
    private_directories(path)
    require_private_state_directory(path, root=root)


def require_private_state_directory(
    path: Path, *, root: Path, missing_ok: bool = False
) -> None:
    """Refuse a state directory another user could tamper with (BL-E008-027).

    ``path`` and, when it lies under ``root`` (the per-user state directory),
    each ancestor up to and including ``root`` must be a real directory — not
    a symlink — owned by this process's effective uid (as :func:`sweep_stale`
    judges ownership; owner decision 44) and not group- or world-writable.
    Whoever owns or can write such a directory could swap a journal, lock or
    Safety Snapshot name for a link. Each refusal is a ``PermissionError``
    (``NotADirectoryError`` for a symlink or a file) whose ``strerror`` names
    the component and the reason; each caller maps it to its own error, as it maps
    :func:`private_directories`' ``OSError``. A directory another user owns
    is named with both uids and :data:`STATE_HOME_HINT`. Off POSIX (no
    ``geteuid``) only the type and mode are checked. ``missing_ok`` skips a
    component that does not exist yet.
    """
    absolute = Path(os.path.abspath(path))  # noqa: PTH100 - lexical, no symlink resolved
    top = Path(os.path.abspath(root))  # noqa: PTH100
    components = [absolute]
    if absolute != top and top in absolute.parents:
        for parent in absolute.parents:
            components.append(parent)
            if parent == top:
                break
    uid = _effective_uid()
    for component in components:
        try:
            info = os.lstat(component)
        except FileNotFoundError:
            if missing_ok:
                continue
            raise
        if not stat.S_ISDIR(info.st_mode):
            kind = "a symlink" if stat.S_ISLNK(info.st_mode) else "not a directory"
            reason = f"state directory {str(component)!r} is {kind}"
            raise NotADirectoryError(errno.ENOTDIR, reason, str(component))
        if uid is not None and info.st_uid != uid:
            reason = (
                f"state directory {str(component)!r} is owned by another user "
                f"(uid {info.st_uid}; this process runs as uid {uid}); "
                f"{STATE_HOME_HINT}"
            )
            raise PermissionError(errno.EPERM, reason, str(component))
        if stat.S_IMODE(info.st_mode) & 0o022:
            reason = (
                f"state directory {str(component)!r} is group- or world-writable "
                f"(mode {stat.S_IMODE(info.st_mode):04o})"
            )
            raise PermissionError(errno.EPERM, reason, str(component))


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

    "This user" is the effective uid, the owner of what the Run creates
    (owner decision 44). A symlink is never followed or removed, nor is
    anything another user owns. ``now`` is a POSIX timestamp. Failures are
    skipped: the sweep is housekeeping, never a reason for the Run to fail.
    Returns what it removed.
    """
    removed: list[Path] = []
    uid = _effective_uid()
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

    Those of ``name`` itself and of any default-named Archive, today's
    ``wpbak-*`` and the legacy ``wpbackup2-*`` (``.<name>.<random>.partial``,
    the store's staging spelling).
    """
    globs = {
        f".{name}.*{STAGED_SUFFIX}",
        *(f".{each}.*{STAGED_SUFFIX}" for each in DEFAULT_ARCHIVE_NAME_GLOBS),
    }
    try:
        candidates = sorted({p for g in globs for p in directory.glob(g)})
    except OSError:
        return ()
    return sweep_stale(candidates, now=now)
