"""AD-3/AD-4 — the database facts the backup ports carry.

Pure values: where a database lives, whether a dump would run, and what a dump
moved. Credentials are never carried here; they are bound into the database
adapter at construction (AD-1, AD-4).

The restore-side values (E002-S02-012): :class:`DatabaseTargetState`, what
the WordPress account, or the administrator where it is needed, observed of a
target (AD-29 as amended by owner decision 55); :class:`EnsureOutcome` and
:class:`LoadResult`, what the restore commands did. Each command result
carries ``performed`` — ``True`` from a real adapter, ``False`` from a no-op
twin — so a check that follows the effect reports "would check" in a Dry Run
rather than re-reading the world (ADR-0022).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

__all__ = [
    "DatabaseLocation",
    "DatabaseTargetState",
    "DumpResult",
    "EnsureOutcome",
    "LoadResult",
    "Readiness",
]


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


@dataclass(frozen=True, slots=True)
class DatabaseTargetState:
    """What was observed of a restore target (AD-29, owner decision 55).

    Database's ``Survey``, copied field by field. ``user_exists`` is ``None``
    when no WordPress user was asked about, or when the observer (the
    WordPress account) cannot read the account table. ``table_count``
    includes views; ``routine_count`` (procedures and functions) and
    ``event_count`` are database's ``Survey`` fields since 0.3.1
    (BL-E003-053), and have no default so no constructor can leave them out.

    ``prefixed_tables`` are the base tables and views whose name starts with
    the site's table prefix: the ones a direct load drops first (owner
    decision 53, database's ``check_drop_tables``). ``None`` when they could
    not be listed (the observer may not drop tables); ``()`` when there are
    none.
    """

    database_exists: bool
    table_count: int
    is_wordpress: bool
    schema_version: str | None
    user_exists: bool | None
    routine_count: int
    event_count: int
    prefixed_tables: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class EnsureOutcome:
    """What ensuring a restore target's database and account did."""

    created_database: bool
    created_user: bool
    password_set: bool
    granted: bool
    warnings: tuple[str, ...]
    performed: bool


@dataclass(frozen=True, slots=True)
class LoadResult:
    """What loading a Dump moved; ``performed`` is ``False`` from a no-op.

    ``dropped`` names the tables and views the load removed first, those
    carrying the site's table prefix (owner decision 53); ``()`` from a
    no-op or a load that pruned nothing.
    """

    bytes: int
    duration_ms: float
    performed: bool
    dropped: tuple[str, ...] = ()
