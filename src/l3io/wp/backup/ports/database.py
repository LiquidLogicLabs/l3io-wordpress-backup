"""AD-4 — the database port, backup half, split into a query and a command.

No method takes a credential: the credential source is bound into the adapter
at construction. The Dump is written to a path, never returned as ``bytes``
(AD-21). E002-S02-012 adds the restore halves.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from l3io.wp.backup.domain.database import DatabaseLocation, DumpResult, Readiness

__all__ = ["DatabaseCommand", "DatabaseQuery"]


class DatabaseQuery(Protocol):
    """What can be learned about a database without changing anything."""

    def check_dump(self, location: DatabaseLocation) -> Readiness:
        """Whether a dump of ``location`` would run, as finding codes."""
        ...


@runtime_checkable
class DatabaseCommand(Protocol):
    """What produces database artefacts."""

    def dump_to(self, location: DatabaseLocation, destination: Path) -> DumpResult:
        """Dump ``location`` to ``destination`` atomically, at mode ``0600``."""
        ...
