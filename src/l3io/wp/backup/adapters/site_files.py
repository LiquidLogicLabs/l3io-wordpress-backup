"""FR-7/FR-9 — the Site Root's restore operations: emptiness, clearing, extraction.

:class:`LocalSiteFiles` implements ``SiteFilesQuery`` and ``SiteFilesCommand``
over the stdlib: ``os.scandir`` counts, ``os.walk`` sizes, ``shutil.rmtree`` /
``os.unlink`` clear. ``extract`` delegates to :func:`adapters.tar.extract_archive`
unchanged — the one AD-11 extraction function; nothing here reads a tar.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path, PurePosixPath
from typing import Final

import structlog

from l3io.wp.backup.adapters.tar import extract_archive
from l3io.wp.backup.domain.archive_format import ArchiveView, ExtractResult, MemberRole
from l3io.wp.backup.domain.errors import (
    CleanFailedError,
    InvalidSiteDescriptorError,
    UnreadableSiteRootError,
    UnsafeCleanTargetError,
)
from l3io.wp.backup.domain.storage_target import StorageTarget
from l3io.wp.backup.ports.site_files import require_site_relative, require_top_level

__all__ = ["LocalSiteFiles", "refuse_unsafe_clean_target"]

_log = structlog.get_logger(__name__)

_NS_PER_MS: Final = 1_000_000
#: What makes a directory look like a WordPress Site Root to ``clean``.
_SITE_MARKERS: Final = ("wp-config.php", "wp-includes")


def _listing(site_root: Path) -> list[os.DirEntry[str]]:
    """``site_root``'s top-level entries; an unusable Site Root is a package error.

    Missing (or not a directory): :class:`InvalidSiteDescriptorError`. Present
    and unreadable (``EACCES`` and the like): :class:`UnreadableSiteRootError`,
    a refusal (BL-E003-034), so FR-7's guard never reads it as empty.
    """
    try:
        with os.scandir(site_root) as entries:
            return list(entries)
    except (FileNotFoundError, NotADirectoryError) as exc:
        reason = exc.strerror or type(exc).__name__
        msg = f"the site root cannot be listed: {reason}"
        raise InvalidSiteDescriptorError(msg, paths=(str(site_root),)) from exc
    except OSError as exc:
        raise UnreadableSiteRootError(site_root, type(exc).__name__) from exc


def refuse_unsafe_clean_target(root: Path) -> None:
    """``UnsafeCleanTargetError`` for a Site Root no clean may empty.

    The filesystem root, the user's home directory, or a directory holding
    neither ``wp-config.php`` nor ``wp-includes/``. A read, never an effect:
    the Dry Run's ``NoopSiteFiles.clean`` calls it too, so a Dry Run refuses
    where the real Run would (AD-4; BL-E002-093).
    """
    resolved = root.resolve()
    if resolved == Path(resolved.anchor):
        raise UnsafeCleanTargetError(root, "it is the filesystem root")
    if resolved == Path.home().resolve():
        raise UnsafeCleanTargetError(root, "it is the user's home directory")
    if not any(os.path.lexists(resolved / marker) for marker in _SITE_MARKERS):
        raise UnsafeCleanTargetError(
            root, "it holds neither wp-config.php nor wp-includes/"
        )


class LocalSiteFiles:
    """The Site Root on the local filesystem."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "LocalSiteFiles()"

    def is_empty(self, site_root: Path) -> bool:
        """Whether ``site_root`` holds no entry."""
        return not _listing(site_root)

    def entry_count(self, site_root: Path) -> int:
        """How many top-level entries ``site_root`` holds."""
        return len(_listing(site_root))

    def file_exists(self, path: Path) -> bool:
        """PHP ``file_exists``: symlinks followed, so a dangling one is ``False``."""
        return os.path.exists(path)  # noqa: PTH110 - False on EACCES, as PHP

    def tree_bytes(self, site_root: Path) -> int:
        """The bytes of every regular file under ``site_root`` (``lstat``).

        Symlinks are counted as themselves and never followed; an entry that
        cannot be read is not counted, so the total is a lower bound.
        """
        total = 0
        for directory, _dirs, files in os.walk(site_root):
            for name in files:
                try:
                    entry = os.path.join(directory, name)  # noqa: PTH118
                    total += os.lstat(entry).st_size
                except OSError:
                    continue
        return total

    def clean(self, site_root: Path, *, keep: frozenset[PurePosixPath]) -> int:
        """Remove every top-level entry of ``site_root`` not in ``keep``.

        ``keep`` names top-level entries (Site-Root-relative); a deeper path
        is refused (``ValueError``, BL-E002-088), since it could only keep its
        top-level ancestor whole. A symlink is unlinked, never
        followed — a symlinked directory's target is untouched — and
        ``site_root`` itself is never removed.

        **A heuristic guard, not a proof.** ``UnsafeCleanTargetError`` is
        raised before anything is removed when ``site_root`` is ``/``, the
        user's home directory, or holds neither ``wp-config.php`` nor
        ``wp-includes/`` — the shapes a mistyped ``--wp-dir`` takes. A
        directory that passes is not thereby proven to be a Site.

        The first ``OSError`` stops the clean: ``CleanFailedError`` names the
        entry, and the entries after it are left in place.
        """
        require_site_relative(keep, argument="keep")
        require_top_level(keep, argument="keep")
        root = site_root.absolute()
        refuse_unsafe_clean_target(root)
        kept = {path.parts[0] for path in keep if path.parts}
        started = time.monotonic_ns()
        removed = 0
        for entry in sorted(_listing(root), key=lambda e: e.name):
            if entry.name in kept:
                continue
            path = Path(entry.path)
            try:
                if entry.is_dir(follow_symlinks=False):
                    shutil.rmtree(path)
                else:
                    path.unlink()
            except OSError as exc:
                reason = exc.strerror or type(exc).__name__
                raise CleanFailedError(path, reason, removed=removed) from exc
            removed += 1
        _log.info(
            "site_root_cleaned",
            path=str(root),
            duration_ms=(time.monotonic_ns() - started) / _NS_PER_MS,
            removed=removed,
            kept=sorted(kept),
        )
        return removed

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
        """``extract_archive``, unchanged (AD-11)."""
        require_site_relative(exclude, argument="exclude")
        return extract_archive(
            source, destination, view, roles=roles, exclude=exclude, payload=payload
        )
