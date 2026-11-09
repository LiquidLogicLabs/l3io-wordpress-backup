"""FR-6, AD-5, AD-17, AD-25 — the restore vocabulary, defined once.

Every epic that restores imports these names; none redefines them.

* :class:`RestoreMode` — FR-6's six Modes, verbatim. What each Mode does is
  read through four pure functions over one ``match`` each, so a Mode added
  without a row is a type error (``assert_never``), not a silent default.
* :class:`RestorePhase` and :data:`RESTORE_PHASE_ORDER` — AD-17's phase order
  as data.
* :class:`Override` and :class:`OverrideSet` — the FR-7 refusals ``--force``
  waives. A distinct type from :class:`RestoreMode`: an ``OverrideSet`` cannot
  name a Mode and cannot authorise a removal.
* :class:`SnapshotPolicy` and :class:`ConfigPolicy` — closed two-value choices
  (``--no-safety-snapshot``, ``--overwrite-wp-config``).

AD-5: no parameter annotated with one of these types carries a default;
``tools/check_mode_defaults.py`` enforces it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum, StrEnum
from typing import Final, Literal, assert_never

__all__ = [
    "RESTORE_PHASE_ORDER",
    "ConfigPolicy",
    "Override",
    "OverrideSet",
    "RestoreMode",
    "RestorePart",
    "RestorePhase",
    "SnapshotPolicy",
    "clears_site_root",
    "is_subsequence",
    "restores_database",
    "restores_files",
]

#: The two parts of a Site a restore can write.
RestorePart = Literal["files", "database"]


class RestoreMode(StrEnum):
    """FR-6's Restore Modes. Resolved once from the flags, carried as a value."""

    FILES = "files"
    FILES_CLEAN = "files-clean"
    DATABASE = "database"
    DATABASE_CLEAN = "database-clean"
    ALL = "all"
    ALL_CLEAN = "all-clean"


def restores_files(mode: RestoreMode) -> bool:
    """Whether ``mode`` writes the Archive's files into the Site Root."""
    match mode:
        case RestoreMode.FILES | RestoreMode.FILES_CLEAN:
            return True
        case RestoreMode.ALL | RestoreMode.ALL_CLEAN:
            return True
        case RestoreMode.DATABASE | RestoreMode.DATABASE_CLEAN:
            return False
        case _:
            assert_never(mode)


def restores_database(mode: RestoreMode) -> bool:
    """Whether ``mode`` loads the Archive's dump into a database."""
    match mode:
        case RestoreMode.DATABASE | RestoreMode.DATABASE_CLEAN:
            return True
        case RestoreMode.ALL | RestoreMode.ALL_CLEAN:
            return True
        case RestoreMode.FILES | RestoreMode.FILES_CLEAN:
            return False
        case _:
            assert_never(mode)


def clears_site_root(mode: RestoreMode) -> bool:
    """Whether ``mode`` clears the Site Root before extracting (``--cleanfirst``)."""
    match mode:
        case RestoreMode.FILES_CLEAN | RestoreMode.ALL_CLEAN:
            return True
        case (
            RestoreMode.FILES
            | RestoreMode.DATABASE
            | RestoreMode.DATABASE_CLEAN
            | RestoreMode.ALL
        ):
            return False
        case _:
            assert_never(mode)


class RestorePhase(Enum):
    """AD-17's restore phases, declared in the order a Run performs them.

    ``REWRITE_CONFIG`` writes no ``DB_*`` key. ``LOAD_DUMP`` loads straight
    into the live database after ``SNAPSHOT`` (owner decision 49) and rolls
    it back from the Safety Snapshot on a failure (decision 50); a
    relocation's cutover (``DB_NAME``, ``DB_HOST``, ``DB_USER``,
    ``DB_PASSWORD``) is its **last journaled step**, taken only after the
    load is checked. Epic 3's phase-sequence test asserts it.

    **Two spellings, one per medium (BL-E002-109, owner decision 2026-11-07).**
    A log record's ``phase`` field and a span name carry the member's
    ``name`` (``LOAD_DUMP``), upper case with underscores like backup's
    ``BackupStep``; the Run Journal stores the ``value`` (``load-dump``), its
    on-disk format unchanged. ``tests/unit/cli/test_phase_names_upper.py``
    asserts every logged ``phase`` is upper case.
    """

    PREFLIGHT = "preflight"
    VERIFY = "verify"
    SNAPSHOT = "snapshot"
    CLEAN = "clean"
    EXTRACT = "extract"
    REWRITE_CONFIG = "rewrite-config"
    LOAD_DUMP = "load-dump"
    SEARCH_REPLACE = "search-replace"


#: AD-17's order, as data.
RESTORE_PHASE_ORDER: Final[tuple[RestorePhase, ...]] = tuple(RestorePhase)


def is_subsequence(phases: Iterable[RestorePhase]) -> bool:
    """Whether ``phases`` occur in AD-17's order, each at most once.

    A Run may skip phases its Mode does not need; it may never reorder or
    repeat one.
    """
    remaining = iter(RESTORE_PHASE_ORDER)
    return all(any(phase is expected for expected in remaining) for phase in phases)


class Override(StrEnum):
    """An FR-7 refusal that ``--force`` can waive."""

    NONEMPTY_FILES = "nonempty-files"
    NONEMPTY_DATABASE = "nonempty-database"


@dataclass(frozen=True, slots=True)
class OverrideSet:
    """The FR-7 refusals waived for a Run — never a Mode, never a removal."""

    waived: frozenset[Override]

    @classmethod
    def none(cls) -> OverrideSet:
        """No refusal waived."""
        return cls(waived=frozenset())

    def waives(self, part: RestorePart) -> bool:
        """Whether the non-empty-target refusal for ``part`` is waived."""
        match part:
            case "files":
                return Override.NONEMPTY_FILES in self.waived
            case "database":
                return Override.NONEMPTY_DATABASE in self.waived
            case _:
                assert_never(part)


class SnapshotPolicy(StrEnum):
    """Whether a restore takes the Safety Snapshot (``--no-safety-snapshot``)."""

    TAKE = "take"
    SKIP = "skip"


class ConfigPolicy(StrEnum):
    """Whether a restore may replace the target's ``wp-config.php`` (ADR-0023)."""

    KEEP = "keep"
    OVERWRITE = "overwrite"
