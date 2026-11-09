"""AD-25 — the restore flags map to ``(RestoreMode, OverrideSet)`` through one table.

The restore subcommand's mode-selecting flags, and what each one means:

* ``--mode {files,db,all}`` chooses the parts restored. The table is keyed by
  the :class:`~l3io.wp.backup.domain.backup_scope.BackupScope` the CLI's
  ``CLI_MODE_TO_SCOPE`` translates it to, so there is no second spelling table.
* ``--cleanfirst`` authorises **clearing the Site Root**, and nothing else.
* ``--drop-database`` is accepted and changes nothing (owner decision 63):
  every database restore loads straight into the live database and drops the
  tables carrying the site's prefix first (decisions 49 and 53), so there is
  no previous database to remove. It still selects ``database-clean`` with
  ``--mode db``, which now behaves as ``database``; with ``--mode files`` or
  ``--mode all`` it selects what the same flags without it select. The CLI
  logs WARNING ``deprecated_option`` when it is given.
* ``--force`` waives FR-7's non-empty-target refusal for exactly the parts the
  Mode restores. It selects no Mode and authorises no removal: it yields an
  :class:`~l3io.wp.backup.domain.restore.OverrideSet`, a distinct type.

:data:`FLAG_MAP` is a **literal** of all 24 combinations, not a comprehension,
so each row can be reviewed against FR-6. It is total — there is no default
branch — and ``tests/contracts/test_flag_map.py`` derives the combinations from
the parser's ``restore mode`` argument group and asserts equality.

Behaviour change from v0.2.21 (B-3): ``--force`` no longer means
``ALLCLEAN``, and ``--cleanfirst`` no longer removes the database. Since
owner decision 63, ``--mode all --cleanfirst`` alone selects ``all-clean``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, TypeAlias

from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.errors import FlagCombinationRejectedError
from l3io.wp.backup.domain.restore import (
    Override,
    OverrideSet,
    RestoreMode,
    RestorePart,
)

__all__ = [
    "COVERED_FLAGS",
    "FLAG_MAP",
    "FlagCombination",
    "FlagEntry",
    "FlagRejection",
    "authorizing_combination",
    "combination",
    "resolve_flags",
]

#: One reachable flag combination, as ``(dest, value)`` pairs sorted by ``dest``.
FlagCombination: TypeAlias = tuple[tuple[str, object], ...]

#: The ``dest`` names of the restore flags this table maps. Asserted equal to
#: the ``restore mode`` argument group of the parser itself.
COVERED_FLAGS: Final[frozenset[str]] = frozenset(
    {"mode", "cleanfirst", "drop_database", "force"}
)


@dataclass(frozen=True, slots=True)
class FlagRejection:
    """A combination that expresses no Restore Mode, and what would."""

    reason: str
    use_instead: str


def combination(
    *, mode: BackupScope, cleanfirst: bool, drop_database: bool, force: bool
) -> FlagCombination:
    """The table key for one set of restore flag values."""
    return (
        ("cleanfirst", cleanfirst),
        ("drop_database", drop_database),
        ("force", force),
        ("mode", mode),
    )


_NONE = OverrideSet.none()
_FILES = OverrideSet(waived=frozenset({Override.NONEMPTY_FILES}))
_DATABASE = OverrideSet(waived=frozenset({Override.NONEMPTY_DATABASE}))
_BOTH = OverrideSet(
    waived=frozenset({Override.NONEMPTY_FILES, Override.NONEMPTY_DATABASE})
)

_DATABASE_CLEAN = FlagRejection(
    reason="--cleanfirst clears the Site Root, which --mode db does not restore",
    use_instead=(
        "use --mode all --cleanfirst to clear the Site Root and restore both; "
        "--mode db alone already replaces every table with the site's prefix"
    ),
)

_F = BackupScope.FILES
_D = BackupScope.DATABASE
_A = BackupScope.ALL

#: AD-25's table, decided 2026-10-20. Every row is FR-6's; nothing re-derives it.
FlagEntry: TypeAlias = tuple[RestoreMode, OverrideSet] | FlagRejection
FLAG_MAP: Final[Mapping[FlagCombination, FlagEntry]] = MappingProxyType(
    {
        # --mode files
        combination(mode=_F, cleanfirst=False, drop_database=False, force=False): (
            RestoreMode.FILES,
            _NONE,
        ),
        combination(mode=_F, cleanfirst=False, drop_database=False, force=True): (
            RestoreMode.FILES,
            _FILES,
        ),
        combination(mode=_F, cleanfirst=True, drop_database=False, force=False): (
            RestoreMode.FILES_CLEAN,
            _NONE,
        ),
        combination(mode=_F, cleanfirst=True, drop_database=False, force=True): (
            RestoreMode.FILES_CLEAN,
            _FILES,
        ),
        # --drop-database changes nothing (owner decision 63).
        combination(mode=_F, cleanfirst=False, drop_database=True, force=False): (
            RestoreMode.FILES,
            _NONE,
        ),
        combination(mode=_F, cleanfirst=False, drop_database=True, force=True): (
            RestoreMode.FILES,
            _FILES,
        ),
        combination(mode=_F, cleanfirst=True, drop_database=True, force=False): (
            RestoreMode.FILES_CLEAN,
            _NONE,
        ),
        combination(mode=_F, cleanfirst=True, drop_database=True, force=True): (
            RestoreMode.FILES_CLEAN,
            _FILES,
        ),
        # --mode db
        combination(mode=_D, cleanfirst=False, drop_database=False, force=False): (
            RestoreMode.DATABASE,
            _NONE,
        ),
        combination(mode=_D, cleanfirst=False, drop_database=False, force=True): (
            RestoreMode.DATABASE,
            _DATABASE,
        ),
        combination(mode=_D, cleanfirst=False, drop_database=True, force=False): (
            RestoreMode.DATABASE_CLEAN,
            _NONE,
        ),
        combination(mode=_D, cleanfirst=False, drop_database=True, force=True): (
            RestoreMode.DATABASE_CLEAN,
            _DATABASE,
        ),
        combination(
            mode=_D, cleanfirst=True, drop_database=False, force=False
        ): _DATABASE_CLEAN,
        combination(
            mode=_D, cleanfirst=True, drop_database=False, force=True
        ): _DATABASE_CLEAN,
        combination(
            mode=_D, cleanfirst=True, drop_database=True, force=False
        ): _DATABASE_CLEAN,
        combination(
            mode=_D, cleanfirst=True, drop_database=True, force=True
        ): _DATABASE_CLEAN,
        # --mode all
        combination(mode=_A, cleanfirst=False, drop_database=False, force=False): (
            RestoreMode.ALL,
            _NONE,
        ),
        combination(mode=_A, cleanfirst=False, drop_database=False, force=True): (
            RestoreMode.ALL,
            _BOTH,
        ),
        combination(mode=_A, cleanfirst=True, drop_database=True, force=False): (
            RestoreMode.ALL_CLEAN,
            _NONE,
        ),
        combination(mode=_A, cleanfirst=True, drop_database=True, force=True): (
            RestoreMode.ALL_CLEAN,
            _BOTH,
        ),
        # Owner decision 63: --cleanfirst alone selects all-clean, and
        # --drop-database changes nothing.
        combination(mode=_A, cleanfirst=True, drop_database=False, force=False): (
            RestoreMode.ALL_CLEAN,
            _NONE,
        ),
        combination(mode=_A, cleanfirst=True, drop_database=False, force=True): (
            RestoreMode.ALL_CLEAN,
            _BOTH,
        ),
        combination(mode=_A, cleanfirst=False, drop_database=True, force=False): (
            RestoreMode.ALL,
            _NONE,
        ),
        combination(mode=_A, cleanfirst=False, drop_database=True, force=True): (
            RestoreMode.ALL,
            _BOTH,
        ),
    }
)


def resolve_flags(flags: FlagCombination) -> tuple[RestoreMode, OverrideSet]:
    """AD-25's one total function: the Mode and waivers ``flags`` express.

    Raises :class:`FlagCombinationRejectedError` (``USAGE``) for a
    combination that expresses no Mode, and ``KeyError`` for one outside the
    table — there is no default.
    """
    entry = FLAG_MAP[flags]
    if isinstance(entry, FlagRejection):
        raise FlagCombinationRejectedError(
            reason=entry.reason, use_instead=entry.use_instead
        )
    return entry


def authorizing_combination(mode: RestoreMode, part: RestorePart) -> FlagCombination:
    """The combination that selects ``mode`` and waives ``part``'s refusal.

    Derived from :data:`FLAG_MAP`, so a refusal message names the waiving flag
    from the table rather than from text typed into the message. Raises
    ``ValueError`` when ``mode`` does not restore ``part``, since then there
    is no refusal to waive. A combination without ``--drop-database`` is
    preferred (owner decision 63: the flag changes nothing).
    """
    ordered = sorted(
        FLAG_MAP.items(), key=lambda item: dict(item[0])["drop_database"] is True
    )
    for flags, entry in ordered:
        if isinstance(entry, FlagRejection):
            continue
        entry_mode, overrides = entry
        if entry_mode is mode and overrides.waives(part):
            return flags
    msg = f"Restore Mode {mode.value!r} does not restore the {part}"
    raise ValueError(msg)
