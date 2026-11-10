"""AD-4/AD-29 — the database port: backup and restore halves, query and command.

No method takes a credential: the WordPress source and, when given, the
administrator source are bound into the adapter at construction. The Dump is
written to a path, never returned as ``bytes`` (AD-21).

**The restore halves (E002-S02-012; owner decisions 53 and 55, ADR-0027).**
A restore loads straight into the live database as the WordPress account:
:meth:`DatabaseQuery.check_load`, :meth:`DatabaseCommand.load_dump` and
:meth:`DatabaseCommand.drop_tables` answer as that account, and
``load_dump(..., replace=True, prune_prefix=<site prefix>)`` first drops every
table and view carrying the site's prefix, so the database holds exactly the
Dump's tables afterwards; tables without the prefix are never touched.
:meth:`DatabaseQuery.survey_target` observes as the WordPress account and
turns to the administrator only when that account cannot see the database
(AD-29 as amended). ``ensure_database``, ``check_ensure`` and
``drop_database`` answer through the bound administrator and raise
``AdminCredentialsRequiredError`` before any connection when none is bound.
``load_dump`` takes no credential or decryption parameter (ADR-0015
amendment, ADR-0024).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import IO, Protocol, runtime_checkable

from l3io.wp.backup.domain.database import (
    DatabaseLocation,
    DatabaseTargetState,
    DumpResult,
    EnsureOutcome,
    LoadResult,
    Readiness,
)

__all__ = ["DatabaseCommand", "DatabaseQuery", "DbTls"]


class DatabaseQuery(Protocol):
    """What can be learned about a database without changing anything."""

    def check_dump(self, location: DatabaseLocation) -> Readiness:
        """Whether a dump of ``location`` would run, as finding codes."""
        ...

    def survey_target(
        self,
        location: DatabaseLocation,
        *,
        table_prefix: str,
        wordpress_user: str | None,
    ) -> DatabaseTargetState:
        """What the WordPress account, else the administrator, observes (AD-29)."""
        ...

    def check_ensure(self, location: DatabaseLocation) -> Readiness:
        """Whether ``ensure_database(location)`` would run."""
        ...

    def check_load(
        self,
        location: DatabaseLocation,
        *,
        source: Path | None,
        replace: bool = False,
        prune_prefix: str | None = None,
    ) -> Readiness:
        """Whether ``load_dump`` into ``location`` with the same options would run."""
        ...


@runtime_checkable
class DatabaseCommand(Protocol):
    """What produces database artefacts or changes a database."""

    def dump_to(self, location: DatabaseLocation, destination: Path) -> DumpResult:
        """Dump ``location`` to ``destination`` atomically, at mode ``0600``."""
        ...

    def ensure_database(self, location: DatabaseLocation) -> EnsureOutcome:
        """Create ``location``'s database and the WordPress account as needed."""
        ...

    def load_dump(
        self,
        location: DatabaseLocation,
        source: IO[bytes] | Path,
        *,
        replace: bool = False,
        prune_prefix: str | None = None,
    ) -> LoadResult:
        """Load the plain Dump ``source`` into ``location.name``.

        Without ``replace`` the database must be empty. With it, the Dump is
        loaded over what is there, after every table and view whose name
        starts with ``prune_prefix`` (when given) was dropped
        (``LoadResult.dropped``).
        """
        ...

    def drop_tables(
        self, location: DatabaseLocation, *, prefix: str
    ) -> tuple[str, ...]:
        """Drop every table and view of ``location`` starting with ``prefix``."""
        ...

    def drop_database(self, location: DatabaseLocation, *, confirm: str) -> bool:
        """Drop ``location``'s database; whether it was performed."""
        ...


@dataclass(frozen=True, slots=True)
class DbTls:
    """The TLS every database connection of a Run uses (owner decision 56).

    ``mode`` is an ``adapters/database.py::DB_TLS_MODES`` value, or ``None``
    for no requirement: the connection is made the way WordPress makes it.
    ``ca_file`` is the CA bundle that verifies the server, ``None`` meaning the
    system store; it needs a verifying ``mode``. A port-level value
    (BL-E003-099; ``Path`` keeps it out of ``domain/``): it holds no
    ``l3io.wp.database`` type (ADR-0015 amendment), so ``RestoreRequest`` and
    the library carry no adapters-layer type; ``adapters/database.py``
    validates and applies it.
    """

    mode: str | None = None
    ca_file: Path | None = None
