"""Owner decision 64 — ``restore --archive latest``: which Archive is "latest".

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

from l3io.wp.backup.domain.archive_format import Manifest, SourceValue, parse_created_at
from l3io.wp.backup.domain.errors import NoArchiveForSiteError
from l3io.wp.backup.domain.site import SiteDescriptor

__all__ = [
    "ArchiveSite",
    "LatestCandidate",
    "LatestChoice",
    "choose_latest",
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


def choose_latest(
    candidates: Sequence[LatestCandidate],
    site: ArchiveSite,
    *,
    directory: PurePosixPath,
) -> LatestChoice:
    """The newest of ``candidates`` that is ``site``'s own backup.

    Raises :class:`NoArchiveForSiteError` when none is.
    """
    matching: list[tuple[datetime, str, PurePosixPath, str]] = []
    for candidate in candidates:
        manifest = candidate.manifest
        if manifest is None or manifest.origin is not None:
            continue
        if ArchiveSite.from_source(manifest.source) != site:
            continue
        created = _created(manifest)
        if created is not None:
            matching.append(
                (created, candidate.path.name, candidate.path, manifest.created_at)
            )
    if not matching:
        raise NoArchiveForSiteError(
            directory=directory, considered=len(candidates), site=site.describe()
        )
    _, _, path, created_at = max(matching, key=lambda item: (item[0], item[1]))
    return LatestChoice(
        path=path,
        created_at=created_at,
        considered=len(candidates),
        matched=len(matching),
    )
