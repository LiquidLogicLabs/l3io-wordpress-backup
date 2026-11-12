"""Owner decision 83 — the Archive directory, as ``prune`` reads and changes it.

:func:`list_archives` reads every ``*.tar.gz`` directly in the directory with
``lstat``: only a regular file is a candidate, its Manifest read from its start
(``adapters/tar.read_leading_manifest``), its device and inode kept. Anything
else (a symlink, a directory, a FIFO) is returned as skipped, with the reason,
never opened. A staged ``.partial`` file does not end in ``.tar.gz``, so it is
never listed: the stale-residue sweep owns those.

:func:`delete_archive` removes one file by name relative to an open
descriptor of the directory (``unlinkat``), after ``lstat`` through the same
descriptor shows it is still the regular file that was read (same device and
inode). A symlink is never followed (``unlink`` removes a link itself, and
the identity check refuses one swapped in); a file replaced since it was read
is left alone. The directory is fsynced after each removal.
"""

from __future__ import annotations

import contextlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

from l3io.wp.backup.adapters.tar import read_leading_manifest
from l3io.wp.backup.domain.archive_format import (
    Manifest,
    ignore_unknown,
    manifest_from_json,
)
from l3io.wp.backup.domain.errors import MalformedManifestError

__all__ = [
    "ARCHIVE_GLOB",
    "ArchiveFile",
    "SkippedFile",
    "delete_archive",
    "list_archives",
]

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


def list_archives(directory: Path) -> tuple[list[ArchiveFile], list[SkippedFile]]:
    """The regular ``*.tar.gz`` files of ``directory`` and the entries skipped."""
    files: list[ArchiveFile] = []
    skipped: list[SkippedFile] = []
    for path in sorted(directory.glob(ARCHIVE_GLOB)):
        where = PurePosixPath(path)
        try:
            info = os.lstat(path)
        except OSError as exc:
            skipped.append(SkippedFile(where, exc.strerror or type(exc).__name__))
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
