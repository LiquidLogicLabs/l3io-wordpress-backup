"""AD-3/AD-4 — the database facts the backup ports carry.

Pure values: where a database lives, whether a dump would run, and what a dump
moved. Credentials are never carried here; they are bound into the database
adapter at construction (AD-1, AD-4). E002-S02-012 adds the restore-side
values to this module.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

__all__ = ["DatabaseLocation", "DumpResult", "Readiness"]


@dataclass(frozen=True, slots=True)
class DatabaseLocation:
    """Where one database lives. Carries no credential."""

    host: str
    port: int
    name: str

    def with_name(self, name: str) -> DatabaseLocation:
        """The same server, another database. The name is never re-parsed."""
        return dataclasses.replace(self, name=name)


@dataclass(frozen=True, slots=True)
class Readiness:
    """Whether a dump would run. Finding **codes** only, never messages."""

    ready: bool
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DumpResult:
    """What a dump moved."""

    bytes: int
    duration_ms: float
