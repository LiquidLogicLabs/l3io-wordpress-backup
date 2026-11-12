"""Owner decisions 64, 83, 84 — "this Site's Archives": ``latest`` and ``prune``.

``restore --archive latest`` names the newest of them (:func:`choose_latest`);
``prune`` keeps those its retention rules keep (``core/retention.py``) and
removes the rest (:func:`choose_prune`). Both read one ordering,
:func:`site_archives`, so there is one definition of which Archives are this
Site's.

The newest, by its Manifest's ``created_at``, of the Archives in the Archive
directory **whose Manifest is for this Site**, so a directory shared by
several sites never yields another site's Archive.

**"This Site", from what the Manifest records.** A v1 Manifest's ``source``
records the Site Root and the database the backup read: ``site_root``,
``db_host``, ``db_port``, ``db_name`` and ``table_prefix``
(``domain/archive_format.source_from_descriptor``). An Archive is this Site's
when all five equal the restore target's, as its ``wp-config.php`` gives them
(the host compared case-insensitively). Two sites that agree on all five
(the same path, database server, database and prefix) cannot be told apart
by a Manifest; give each its own Archive directory.

Only an operator's backup counts: a Safety Snapshot (a Manifest with an
``origin``) is never "latest". An Archive whose Manifest could not be read,
or whose ``created_at`` does not parse, is skipped. Ties on ``created_at``
are broken by the file name, so the choice is deterministic.

Pure: the CLI root lists the directory and reads each Manifest
(``adapters/tar.read_leading_manifest``); nothing here does I/O.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath

from l3io.wp.backup.core.retention import RetentionPolicy, apply_retention
from l3io.wp.backup.domain.archive_format import Manifest, SourceValue, parse_created_at
from l3io.wp.backup.domain.errors import NoArchiveForSiteError
from l3io.wp.backup.domain.site import SiteDescriptor

__all__ = [
    "ArchiveSite",
    "ChosenArchive",
    "LatestCandidate",
    "LatestChoice",
    "PruneChoice",
    "SiteArchive",
    "choose_latest",
    "choose_prune",
    "site_archives",
]


@dataclass(frozen=True, slots=True)
class ArchiveSite:
    """What identifies a Site in a Manifest: its root and its database."""

    site_root: str
    db_host: str
    db_port: int
    db_name: str
    table_prefix: str

    @classmethod
    def from_descriptor(cls, site: SiteDescriptor) -> ArchiveSite:
        """The restore target's identity, from its ``wp-config.php``."""
        return cls(
            site_root=str(site.site_root),
            db_host=site.db_host.lower(),
            db_port=site.db_port,
            db_name=site.db_name,
            table_prefix=site.table_prefix,
        )

    @classmethod
    def from_source(cls, source: Mapping[str, SourceValue]) -> ArchiveSite | None:
        """A Manifest's ``source`` as an identity; ``None`` if a part is missing."""
        site_root, host, port = (
            source.get("site_root"),
            source.get("db_host"),
            source.get("db_port"),
        )
        name, prefix = source.get("db_name"), source.get("table_prefix")
        if not (
            isinstance(site_root, str)
            and isinstance(host, str)
            and isinstance(port, int)
            and not isinstance(port, bool)
            and isinstance(name, str)
            and isinstance(prefix, str)
        ):
            return None
        return cls(
            site_root=site_root,
            db_host=host.lower(),
            db_port=port,
            db_name=name,
            table_prefix=prefix,
        )

    def describe(self) -> str:
        """The identity as a refusal names it; never a credential."""
        return (
            f"Site Root {self.site_root}, database {self.db_name} on "
            f"{self.db_host}:{self.db_port}, table prefix {self.table_prefix}"
        )


@dataclass(frozen=True, slots=True)
class LatestCandidate:
    """One Archive in the directory and its Manifest (``None``: unreadable)."""

    path: PurePosixPath
    manifest: Manifest | None


@dataclass(frozen=True, slots=True)
class LatestChoice:
    """The Archive ``latest`` names, and how it was chosen."""

    path: PurePosixPath
    created_at: str
    considered: int
    matched: int


def _created(manifest: Manifest) -> datetime | None:
    try:
        return parse_created_at(manifest.created_at)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class SiteArchive:
    """One of this Site's Archives: its path and its Manifest's ``created_at``.

    ``created_at`` as the Manifest writes it; ``created`` parsed, in UTC.
    """

    path: PurePosixPath
    created_at: str
    created: datetime


def site_archives(
    candidates: Sequence[LatestCandidate], site: ArchiveSite
) -> list[SiteArchive]:
    """``site``'s own backups among ``candidates``, newest first.

    A Safety Snapshot (a Manifest with an ``origin``), an unreadable Manifest,
    another site's and an unparsable ``created_at`` are left out. Ordered by
    ``created_at``, a tie by the later file name.
    """
    matching: list[tuple[datetime, str, SiteArchive]] = []
    for candidate in candidates:
        manifest = candidate.manifest
        if manifest is None or manifest.origin is not None:
            continue
        if ArchiveSite.from_source(manifest.source) != site:
            continue
        created = _created(manifest)
        if created is not None:
            archive = SiteArchive(
                path=candidate.path, created_at=manifest.created_at, created=created
            )
            matching.append((created, candidate.path.name, archive))
    matching.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [archive for _, _, archive in matching]


def choose_latest(
    candidates: Sequence[LatestCandidate],
    site: ArchiveSite,
    *,
    directory: PurePosixPath,
) -> LatestChoice:
    """The newest of ``candidates`` that is ``site``'s own backup.

    Raises :class:`NoArchiveForSiteError` when none is.
    """
    matching = site_archives(candidates, site)
    if not matching:
        raise NoArchiveForSiteError(
            directory=directory, considered=len(candidates), site=site.describe()
        )
    newest = matching[0]
    return LatestChoice(
        path=newest.path,
        created_at=newest.created_at,
        considered=len(candidates),
        matched=len(matching),
    )


@dataclass(frozen=True, slots=True)
class ChosenArchive:
    """One of this Site's Archives a prune keeps or removes, and why.

    Kept: ``reasons`` are the rules that keep it (``("daily", "monthly")``).
    Removed: one reason per rule given (``"daily: not the newest of its day"``).
    """

    path: PurePosixPath
    created_at: str
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PruneChoice:
    """What ``prune`` keeps and removes, newest first in each."""

    kept: tuple[ChosenArchive, ...]
    pruned: tuple[ChosenArchive, ...]
    considered: int
    matched: int


def choose_prune(
    candidates: Sequence[LatestCandidate],
    site: ArchiveSite,
    *,
    policy: RetentionPolicy,
) -> PruneChoice:
    """Keep what ``policy`` keeps of ``site``'s Archives; the rest of its own go.

    A :class:`RetentionPolicy` has at least one rule, each at least 1, so the
    newest is always kept. Nothing that is not ``site``'s own backup
    (:func:`site_archives`) is ever in ``pruned``. Ties are broken by the
    later path, which in one directory is :func:`site_archives`' later name.
    """
    matching = site_archives(candidates, site)
    by_name = {str(a.path): a for a in matching}
    kept: list[ChosenArchive] = []
    pruned: list[ChosenArchive] = []
    for verdict in apply_retention(
        [(a.created, str(a.path)) for a in matching], policy
    ):
        archive = by_name[verdict.name]
        if verdict.kept_by:
            kept.append(
                ChosenArchive(archive.path, archive.created_at, verdict.kept_by)
            )
        else:
            pruned.append(
                ChosenArchive(archive.path, archive.created_at, verdict.pruned_because)
            )
    return PruneChoice(
        kept=tuple(kept),
        pruned=tuple(pruned),
        considered=len(candidates),
        matched=len(matching),
    )
