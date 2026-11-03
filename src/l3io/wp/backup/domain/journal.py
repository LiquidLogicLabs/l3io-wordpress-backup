"""AD-18.1 — the Run Journal's values (ADR-0020, ADR-0021).

Pure values; the durable write sequence and the file layout are
``adapters/journal.py``'s (Epic 3, E003-S02-005).

:class:`JournalHeader` is what a restore records before its first effect.
``cutover_targets`` and ``prior_constants`` may hold only
:data:`JOURNALED_CONSTANTS`, so ``DB_PASSWORD`` cannot be journaled; the
header's mappings are read-only, so no key is added after that check.
``phase_undo`` is Epic 7's per-phase undo text and ``saved_config`` the path of
the config copy ``--overwrite-wp-config`` saves, so a later Run derives the
undo from the record alone.

:class:`JournalEntry` stores no undo: ``step`` names the step a
``step_completed`` entry completed, and the undo is derived when the journal is
read (ADR-0020).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final, Literal, NoReturn, TypeVar

from l3io.wp.backup.domain.restore import RestoreMode, RestorePhase

__all__ = [
    "JOURNALED_CONSTANTS",
    "JournalEntry",
    "JournalEvent",
    "JournalHeader",
    "JournalRecord",
    "ReadOnlyDict",
]

#: The only ``wp-config.php`` constants a journal may hold. Never a secret.
JOURNALED_CONSTANTS: Final = frozenset({"DB_NAME", "DB_HOST", "DB_USER"})

#: What a journal entry records.
JournalEvent = Literal["phase_started", "step_completed", "phase_completed"]


_K = TypeVar("_K")
_V = TypeVar("_V")


class ReadOnlyDict(dict[_K, _V]):
    """A ``dict`` no one can change after it is built.

    Why not ``types.MappingProxyType``: ``dataclasses.asdict`` deep-copies any
    value that is not a ``dict``, and a ``mappingproxy`` cannot be copied, so
    the header would no longer round-trip (the story's testability AC). Python
    3.11 has no ``frozendict``. Being a ``dict``, ``asdict`` rebuilds it with
    ``type(obj)(pairs)``; ``__reduce__`` lets ``copy``/``pickle`` do the same.
    """

    __slots__ = ()

    def _refuse(self, *_: object, **__: object) -> NoReturn:
        msg = "a journal mapping is read-only"
        raise TypeError(msg)

    __setitem__ = __delitem__ = _refuse
    clear = pop = popitem = setdefault = update = _refuse
    __ior__ = _refuse

    def __hash__(self) -> int:  # type: ignore[override]
        return hash(frozenset(self.items()))

    def __reduce__(self) -> tuple[type[ReadOnlyDict[_K, _V]], tuple[dict[_K, _V]]]:
        return (type(self), (dict(self),))


def _journaled(field: str, keys: Mapping[str, object]) -> None:
    refused = sorted(set(keys) - JOURNALED_CONSTANTS)
    if refused:
        msg = (
            f"{field} may hold only {sorted(JOURNALED_CONSTANTS)}; "
            f"refused keys: {refused}"
        )
        raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class JournalHeader:
    """What a restore records before its first effect (AD-18.1)."""

    run_id: str
    started_at: str
    site_root: PurePosixPath
    restore_mode: RestoreMode
    archive: str
    snapshot_path: str | None
    live_database_before: str
    staging_database: str | None
    wp_config_path: str
    load_database: str
    cutover_keys: tuple[str, ...]
    cutover_targets: Mapping[str, str]
    prior_constants: Mapping[str, str | None]
    phase_undo: Mapping[RestorePhase, str]
    saved_config: str | None

    def __post_init__(self) -> None:
        # Key names only in the message: a refused key's value is never shown.
        _journaled("cutover_targets", self.cutover_targets)
        _journaled("prior_constants", self.prior_constants)
        # Read-only copies: neither the caller's later mutation nor a write
        # through the header can add a key after the check above.
        object.__setattr__(self, "cutover_keys", tuple(self.cutover_keys))
        for name in ("cutover_targets", "prior_constants", "phase_undo"):
            object.__setattr__(self, name, ReadOnlyDict(getattr(self, name)))


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """One journal event. No undo is stored; it is derived on read (ADR-0020)."""

    event: JournalEvent
    phase: RestorePhase
    at: str
    target: str | None
    step: str | None


@dataclass(frozen=True, slots=True)
class JournalRecord:
    """A journal as read back: its header and its entries, in order."""

    header: JournalHeader
    entries: tuple[JournalEntry, ...]
