"""AD-4/AD-29 — the database port: backup and restore halves, query and command.

No method takes a credential: the WordPress source and, when given, the
administrator source are bound into the adapter at construction. The Dump is
written to a path, never returned as ``bytes`` (AD-21).

**The restore halves (E002-S02-012).** Every restore-half method except
:meth:`DatabaseCommand.load_dump` answers through the bound administrator and
raises ``AdminCredentialsRequiredError`` before any connection when none is
bound — existence is answered by the administrator, never inferred from a
failed login (AD-29). ``load_dump`` loads as the bound WordPress account into
``location.name``; it has no ``replace``, credential or decryption parameter,
so loading over a non-empty database cannot be expressed through the port
(AD-18.3, ADR-0015 amendment, ADR-0024).
"""

from __future__ import annotations

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

__all__ = ["DatabaseCommand", "DatabaseQuery"]


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
        """What the administrator observes of ``location`` (AD-29)."""
        ...

    def check_ensure(self, location: DatabaseLocation) -> Readiness:
        """Whether ``ensure_database(location)`` would run."""
        ...

    def check_load(
        self, location: DatabaseLocation, *, source: Path | None
    ) -> Readiness:
        """Whether ``load_dump`` into ``location`` would run."""
        ...

    def check_revoke(
        self, location: DatabaseLocation, *, user: str, confirm: str
    ) -> Readiness:
        """Whether ``revoke_grants(location, user=user, ...)`` would run."""
        ...

    def check_drop_database(
        self, location: DatabaseLocation, *, confirm: str
    ) -> Readiness:
        """Whether ``drop_database(location, ...)`` would run."""
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
        self, location: DatabaseLocation, source: IO[bytes] | Path
    ) -> LoadResult:
        """Load the plain Dump ``source`` into the empty ``location.name``."""
        ...

    def revoke_grants(
        self, location: DatabaseLocation, *, user: str, confirm: str
    ) -> bool:
        """Revoke ``user``'s grants on ``location``; whether it was performed."""
        ...

    def drop_database(self, location: DatabaseLocation, *, confirm: str) -> bool:
        """Drop ``location``'s database; whether it was performed."""
        ...
