"""Owner decisions 83, 86 — the Archive directory, as ``prune`` and ``latest`` read it.

:func:`list_archives` reads the Archive directory **as a tree** (owner
decision 86: Archives may sit in ``archive_subdir`` folders), with
``os.scandir`` and ``lstat`` only. Every entry whose name matches
:data:`ARCHIVE_GLOB` is looked at: only a regular file is a candidate, its
Manifest read from its start (``adapters/tar.read_leading_manifest``), its
device and inode kept. Anything else (a symlink, a directory, a FIFO) is
returned as skipped, with the reason, never opened. A staged ``.partial``
file does not end in ``.tar.gz``, so it is never listed: the stale-residue
sweep owns those.

**The walk's bounds.** A symlinked directory is never followed (reported as
skipped), so the walk stays inside the Archive directory and cannot loop. It
goes at most :data:`~l3io.wp.backup.domain.archive_subdir.ARCHIVE_TREE_DEPTH`
folder levels down, the deepest an ``archive_subdir`` template may write; a
folder below that is reported, not entered. It does not enter a Run
workspace (``l3io-wp-backup-*``) or a directory the caller names in
``skip_dirs`` (the state directory, which holds the Safety Snapshots) below
the Archive directory. A folder that cannot be read is reported, not raised.
Which Archives are a site's is still decided by Manifest, never by name or
folder (``core/archive_choice``).

:func:`delete_archive` removes one file by name relative to an open
descriptor of the directory (``unlinkat``), after ``lstat`` through the same
descriptor shows it is still the regular file that was read (same device and
inode). A symlink is never followed (``unlink`` removes a link itself, and
the identity check refuses one swapped in); a file replaced since it was read
is left alone. The directory is fsynced after each removal.

:func:`remove_emptied_directories` then removes the folders a prune emptied:
on the path from a removed Archive up to, never including, the Archive
directory, deepest first, each only while it is a real directory (``lstat``)
and empty (``rmdir`` refuses one that is not).

:func:`archive_subdirectory` is the backup's half: the folder an
``archive_subdir`` resolves to, refused when a folder on the way already
exists as a symlink or a non-directory, so an Archive is never written
anywhere the walk would not find it.
"""

from __future__ import annotations

import contextlib
import errno
import fnmatch
import os
import stat
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

import structlog

from l3io.wp.backup.adapters.lock import state_root
from l3io.wp.backup.adapters.tar import read_leading_manifest
from l3io.wp.backup.adapters.workspace import (
    WORKSPACE_PREFIX,
    staged_globs,
    sweep_stale,
)
from l3io.wp.backup.domain.archive_format import (
    Manifest,
    ignore_unknown,
    manifest_from_json,
)
from l3io.wp.backup.domain.archive_subdir import ARCHIVE_TREE_DEPTH
from l3io.wp.backup.domain.errors import MalformedManifestError, UsageError

__all__ = [
    "ARCHIVE_GLOB",
    "ArchiveFile",
    "SkippedFile",
    "archive_subdirectory",
    "delete_archive",
    "find_in_tree",
    "list_archives",
    "remove_emptied_directories",
    "sweep_stale_partials_in_tree",
]

_log = structlog.get_logger(__name__)

#: What an Archive's name ends in, whatever else it is called.
ARCHIVE_GLOB: Final = "*.tar.gz"


@dataclass(frozen=True, slots=True)
class ArchiveFile:
    """A regular ``*.tar.gz`` file in the directory, and its Manifest."""

    path: PurePosixPath
    manifest: Manifest | None
    size: int
    identity: tuple[int, int]

    def __repr__(self) -> str:
        """The path and size; the Manifest is not shown."""
        return f"ArchiveFile(path={str(self.path)!r}, size={self.size})"


@dataclass(frozen=True, slots=True)
class SkippedFile:
    """A ``*.tar.gz`` entry that is not a candidate, and why."""

    path: PurePosixPath
    reason: str


def _manifest(path: Path) -> Manifest | None:
    text = read_leading_manifest(path)
    if text is None:
        return None
    try:
        return manifest_from_json(text, on_unknown=ignore_unknown)
    except MalformedManifestError:
        return None


def _reason(exc: OSError) -> str:
    return exc.strerror or type(exc).__name__


def find_in_tree(
    root: Path,
    match: Callable[[str], bool],
    *,
    skip_dirs: Collection[Path] = (),
) -> tuple[list[Path], list[SkippedFile]]:
    """Every entry under ``root`` whose name ``match``es (a symlinked directory
    excepted: it is reported), and what was not entered, both sorted by path.
    The walk's bounds are the module's; the caller ``lstat``s what it found.

    ``root`` itself is always read; a ``skip_dirs`` entry is compared after
    ``resolve`` and only skips a directory strictly below ``root``.
    """
    skipped_dirs = {Path(d).resolve() for d in skip_dirs}
    found: list[Path] = []
    skipped: list[SkippedFile] = []
    pending: list[tuple[Path, int]] = [(root, 0)]
    while pending:
        directory, depth = pending.pop()
        try:
            with os.scandir(directory) as entries:
                listed = list(entries)
        except OSError as exc:
            skipped.append(SkippedFile(PurePosixPath(directory), _reason(exc)))
            continue
        for entry in listed:
            path = directory / entry.name
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                is_link_to_dir = not is_dir and entry.is_symlink() and entry.is_dir()
            except OSError as exc:
                skipped.append(SkippedFile(PurePosixPath(path), _reason(exc)))
                continue
            if is_link_to_dir:
                skipped.append(
                    SkippedFile(PurePosixPath(path), "a symlinked directory")
                )
            elif is_dir:
                if entry.name.startswith(WORKSPACE_PREFIX):
                    continue
                if skipped_dirs and path.resolve() in skipped_dirs:
                    continue
                if depth >= ARCHIVE_TREE_DEPTH:
                    skipped.append(
                        SkippedFile(
                            PurePosixPath(path),
                            f"deeper than {ARCHIVE_TREE_DEPTH} levels",
                        )
                    )
                    continue
                pending.append((path, depth + 1))
            if match(entry.name) and not is_link_to_dir:
                found.append(path)
    found.sort()
    skipped.sort(key=lambda s: s.path)
    return found, skipped


def list_archives(
    directory: Path, *, skip_dirs: Collection[Path] | None = None
) -> tuple[list[ArchiveFile], list[SkippedFile]]:
    """The regular ``*.tar.gz`` files under ``directory`` and the entries skipped.

    Flat and in subfolders, within the walk's bounds (the module docstring).
    ``skip_dirs=None`` skips this user's state directory (``adapters/lock.
    state_root``: the Safety Snapshots, journals and locks) when it is below
    ``directory``.
    """
    files: list[ArchiveFile] = []
    paths, skipped = find_in_tree(
        directory,
        lambda name: fnmatch.fnmatchcase(name, ARCHIVE_GLOB),
        skip_dirs=(state_root(),) if skip_dirs is None else skip_dirs,
    )
    for path in paths:
        where = PurePosixPath(path)
        try:
            info = os.lstat(path)
        except OSError as exc:
            skipped.append(SkippedFile(where, _reason(exc)))
            continue
        if stat.S_ISLNK(info.st_mode):
            skipped.append(SkippedFile(where, "a symlink"))
            continue
        if not stat.S_ISREG(info.st_mode):
            skipped.append(SkippedFile(where, "not a regular file"))
            continue
        files.append(
            ArchiveFile(
                path=where,
                manifest=_manifest(path),
                size=info.st_size,
                identity=(info.st_dev, info.st_ino),
            )
        )
    skipped.sort(key=lambda s: s.path)
    return files, skipped


def delete_archive(archive: ArchiveFile) -> str | None:
    """Remove ``archive``; ``None`` when removed, else why it was left alone."""
    path = Path(archive.path)
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    try:
        directory = os.open(path.parent, flags)
    except OSError as exc:
        return exc.strerror or type(exc).__name__
    try:
        try:
            info = os.lstat(path.name, dir_fd=directory)
        except OSError as exc:
            return exc.strerror or type(exc).__name__
        if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != (
            archive.identity
        ):
            return "changed since it was read"
        try:
            os.unlink(path.name, dir_fd=directory)
        except OSError as exc:
            return exc.strerror or type(exc).__name__
        # Removed; a directory that cannot be fsynced has still lost it.
        with contextlib.suppress(OSError):
            os.fsync(directory)
        return None
    finally:
        os.close(directory)


def remove_emptied_directories(
    root: Path, removed: Iterable[PurePosixPath]
) -> tuple[Path, ...]:
    """Remove the folders under ``root`` that ``removed`` Archives left empty.

    Only the folders on the path from each removed Archive up to ``root``,
    never ``root`` itself, deepest first. Each must still be a directory and
    not a symlink (``lstat``), and ``rmdir`` removes only an empty one: a
    folder holding anything (another Archive, a ``.partial``) stays, and so
    do the folders above it. Each removal logs INFO ``archive_dir_removed``;
    a failure other than "not empty" is logged at DEBUG and skipped.
    Returns what was removed, in order.
    """
    candidates: set[Path] = set()
    for archive in removed:
        path = Path(archive)
        if not path.is_relative_to(root):
            continue
        for parent in path.parents:
            if parent == root or not parent.is_relative_to(root):
                break
            candidates.add(parent)
    done: list[Path] = []
    blocked: set[Path] = set()
    for directory in sorted(candidates, key=lambda d: (-len(d.parts), d)):
        if any(b.is_relative_to(directory) for b in blocked):
            blocked.add(directory)
            continue
        try:
            info = os.lstat(directory)
            if not stat.S_ISDIR(info.st_mode):
                blocked.add(directory)
                continue
            directory.rmdir()
        except OSError as exc:
            if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                _log.debug(
                    "archive_dir_not_removed", path=str(directory), reason=_reason(exc)
                )
            blocked.add(directory)
            continue
        _log.info("archive_dir_removed", path=str(directory))
        done.append(directory)
    return tuple(done)


def archive_subdirectory(root: Path, relative: PurePosixPath) -> Path:
    """``root / relative``, the folder a backup's Archive goes in.

    Each folder on the way that exists must be a real directory: a symlink
    (which ``latest`` and ``prune`` would not follow, and which could lead
    out of ``root``) or a file is :class:`UsageError`. The missing ones are
    made by the store when it stages. ``relative`` is a resolved
    ``archive_subdir`` (no ``..``, ``.`` or empty segment).
    """
    target = root
    for part in relative.parts:
        target = target / part
        try:
            info = os.lstat(target)
        except FileNotFoundError:
            break
        except OSError as exc:
            msg = f"archive subfolder {target} cannot be checked: {_reason(exc)}"
            raise UsageError(msg, arguments=("archive_subdir",)) from exc
        if stat.S_ISLNK(info.st_mode):
            msg = (
                f"archive subfolder {target} is a symlink; archives are written "
                "only into real folders under the archive directory, which "
                "latest and prune search without following symlinks"
            )
            raise UsageError(msg, arguments=("archive_subdir",))
        if not stat.S_ISDIR(info.st_mode):
            msg = f"archive subfolder {target} exists and is not a directory"
            raise UsageError(msg, arguments=("archive_subdir",))
    return root / Path(*relative.parts)


def sweep_stale_partials_in_tree(
    root: Path, name: str, *, now: float
) -> tuple[Path, ...]:
    """``adapters/workspace.sweep_stale`` over the staged files under ``root``.

    The staged files of Archive ``name`` and of every default name
    (``workspace.staged_globs``), in ``root`` and its subfolders within the
    walk's bounds (owner decision 86): a Run killed in last month's
    ``archive_subdir`` folder is cleaned up by this month's.
    """
    globs = staged_globs(name)
    candidates, _ = find_in_tree(
        root, lambda entry: any(fnmatch.fnmatchcase(entry, g) for g in globs)
    )
    return sweep_stale(candidates, now=now)
