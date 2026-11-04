"""AD-4/AD-11 — the Site Root port, split into a query and a command (FR-7, FR-9).

Paths in ``keep`` and ``exclude`` are Site-Root-relative POSIX paths the
caller decides; :func:`require_site_relative` is the one check the real
adapter (E002-S02-009) and the no-op twin both apply before anything is
removed or written. ``extract``'s real implementation delegates to
``adapters.tar.extract_archive`` unchanged, the single AD-11 extraction
function; ``payload`` is the decrypted payload of an encrypted Archive
(ADR-0024).
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Protocol, runtime_checkable

from l3io.wp.backup.domain.archive_format import ArchiveView, ExtractResult, MemberRole
from l3io.wp.backup.domain.storage_target import StorageTarget

__all__ = ["SiteFilesCommand", "SiteFilesQuery", "require_site_relative"]


def require_site_relative(paths: Iterable[PurePosixPath], *, argument: str) -> None:
    """``ValueError`` when any path is absolute or has a ``..`` part.

    A programming error, raised before any effect: the caller built the set.
    """
    for path in paths:
        if path.is_absolute() or ".." in path.parts:
            msg = f"{argument} must hold Site-Root-relative paths, got {str(path)!r}"
            raise ValueError(msg)


class SiteFilesQuery(Protocol):
    """What can be learned about a Site Root without changing it."""

    def is_empty(self, site_root: Path) -> bool:
        """Whether ``site_root`` holds no entry."""
        ...

    def entry_count(self, site_root: Path) -> int:
        """How many top-level entries ``site_root`` holds (FR-7's ``detail``)."""
        ...

    def file_exists(self, path: Path) -> bool:
        """PHP ``file_exists`` semantics: symlinks are followed."""
        ...

    def tree_bytes(self, site_root: Path) -> int:
        """The bytes of the files under ``site_root`` (ADV-6's snapshot need)."""
        ...


@runtime_checkable
class SiteFilesCommand(Protocol):
    """What changes a Site Root."""

    def clean(self, site_root: Path, *, keep: frozenset[PurePosixPath]) -> int:
        """Remove every entry but ``keep``; how many were removed.

        The Site Root itself is never removed.
        """
        ...

    def extract(
        self,
        source: StorageTarget,
        destination: Path,
        view: ArchiveView,
        *,
        roles: frozenset[MemberRole],
        exclude: frozenset[PurePosixPath] = frozenset(),
        payload: Path | None = None,
    ) -> ExtractResult:
        """Extract ``roles``' members of ``view`` into ``destination`` (AD-11)."""
        ...
