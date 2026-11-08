"""ADR-0020, ADR-0021 — a restore's journal steps, read back.

Split from ``core/restore.py`` (BL-E003-109). The named journal steps every
story that journals or reads a step uses (``STEP_*``);
:func:`is_orderly_failure`, whether a failed Run left the Site as it was;
and the read-back of an interrupted Run's journal: the phase it stopped in
(:func:`interrupted_phase`), its last step, the cutover's progress, the undo
text an operator is given (:func:`undo_for`) and the refusal that names it
(:class:`IncompleteRunError`).

Pure: the journal's records are values here; ``adapters/journal.py`` reads
and writes the file.
"""

from __future__ import annotations

from typing import Final, assert_never

from l3io.wp.backup.domain.errors import RefusedError
from l3io.wp.backup.domain.journal import JournalEntry, JournalHeader, JournalRecord
from l3io.wp.backup.domain.restore import (
    RestorePhase,
)

__all__ = [
    "STEP_CHECK_FAILED",
    "STEP_CONFIG",
    "STEP_CONFIG_REPLACED",
    "STEP_CONFIG_SAVED",
    "STEP_CONFIG_STARTED",
    "STEP_CUTOVER",
    "STEP_CUTOVER_READBACK_FAILED",
    "STEP_CUTOVER_STARTED",
    "STEP_LOSER_DROPPED",
    "STEP_LOSER_DROP_STARTED",
    "STEP_STAGING_DROPPED",
    "IncompleteRunError",
    "cutover_progress",
    "incomplete_run_error",
    "interrupted_phase",
    "is_orderly_failure",
    "last_step",
    "replaced_files",
    "undo_for",
]

# The named journal steps of ADR-0020. Every story that journals or reads a
# step uses these constants.
STEP_CONFIG_STARTED: Final = "config_started"
STEP_CONFIG: Final = "config"
STEP_CONFIG_SAVED: Final = "config_saved"
STEP_CONFIG_REPLACED: Final = "config_replaced"
#: Prefix: followed by the key name, e.g. ``"cutover_started:DB_NAME"``.
STEP_CUTOVER_STARTED: Final = "cutover_started:"
#: Prefix: followed by the key name, e.g. ``"cutover:DB_NAME"``.
STEP_CUTOVER: Final = "cutover:"
STEP_CHECK_FAILED: Final = "check_failed"
STEP_CUTOVER_READBACK_FAILED: Final = "cutover_readback_failed"
STEP_STAGING_DROPPED: Final = "staging_dropped"
STEP_LOSER_DROP_STARTED: Final = "loser_drop_started"
STEP_LOSER_DROPPED: Final = "loser_dropped"


# --------------------------------------------------------------------------
# The orderly-failure predicate (ADR-0021)
# --------------------------------------------------------------------------

_FILE_PHASES: Final = frozenset(
    {RestorePhase.CLEAN, RestorePhase.EXTRACT, RestorePhase.REWRITE_CONFIG}
)


def is_orderly_failure(entries: tuple[JournalEntry, ...]) -> bool:
    """Whether a failed Run left the Site as it found it (ADR-0021).

    True only when no ``CLEAN``, ``EXTRACT`` or ``REWRITE_CONFIG`` phase
    started, no ``"cutover:*"`` step completed, ``"staging_dropped"``
    completed, and ``"check_failed"`` or ``"cutover_readback_failed"`` did.
    """
    steps = {e.step for e in entries if e.event == "step_completed" and e.step}
    if any(e.event == "phase_started" and e.phase in _FILE_PHASES for e in entries):
        return False
    if any(step.startswith(STEP_CUTOVER) for step in steps):
        return False
    return STEP_STAGING_DROPPED in steps and bool(
        steps & {STEP_CHECK_FAILED, STEP_CUTOVER_READBACK_FAILED}
    )


# --------------------------------------------------------------------------
# Reading a journal back: the interrupted phase and its undo (ADR-0020)
# --------------------------------------------------------------------------


def interrupted_phase(entries: tuple[JournalEntry, ...]) -> RestorePhase | None:
    """The phase a Run was in when it stopped, from its journal ``entries``.

    The last ``phase_started`` phase with no ``phase_completed`` after it;
    when every started phase completed, the last completed one (the Run died
    between phases, or before ``finish_run``); ``None`` when none started.
    """
    started = [i for i, e in enumerate(entries) if e.event == "phase_started"]
    if not started:
        return None
    last = started[-1]
    current = entries[last].phase
    if any(e.event == "phase_completed" and e.phase is current for e in entries[last:]):
        completed = [e.phase for e in entries if e.event == "phase_completed"]
        return completed[-1]
    return current


def last_step(
    entries: tuple[JournalEntry, ...], phase: RestorePhase | None
) -> str | None:
    """The ``step`` of the last ``step_completed`` entry of ``phase``, or ``None``."""
    if phase is None:
        return None
    steps = [
        e.step
        for e in entries
        if e.event == "step_completed" and e.phase is phase and e.step
    ]
    return steps[-1] if steps else None


def cutover_progress(
    entries: tuple[JournalEntry, ...], header: JournalHeader
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(written, pending)``: ``header.cutover_keys`` split by ``"cutover:<KEY>"``.

    A key whose ``"cutover_started:<KEY>"`` is the last entry is pending: it
    may or may not have landed, and resolution re-reads it.
    """
    landed = {
        e.step.removeprefix(STEP_CUTOVER)
        for e in entries
        if e.event == "step_completed"
        and e.phase is RestorePhase.LOAD_DUMP
        and e.step
        and e.step.startswith(STEP_CUTOVER)
    }
    written = tuple(k for k in header.cutover_keys if k in landed)
    pending = tuple(k for k in header.cutover_keys if k not in landed)
    return written, pending


def _resolve_hint(header: JournalHeader) -> str:
    return f"resolve with `--resolve-interrupted-run {header.run_id}`"


def _snapshot_undo(header: JournalHeader) -> str:
    if header.snapshot_path is None:
        return "no Safety Snapshot: opted out"
    return f"restore files from Safety Snapshot {header.snapshot_path}"


def _prior_values(header: JournalHeader) -> str:
    return (
        "the values in prior_constants (DB_PASSWORD: from Safety Snapshot "
        f"{header.snapshot_path or '(none: opted out)'})"
    )


def replaced_files(entries: tuple[JournalEntry, ...]) -> bool:
    """Whether a ``CLEAN``, ``EXTRACT`` or ``REWRITE_CONFIG`` phase started.

    A Mode that restores files runs them before ``LOAD_DUMP``, so a Run
    stopped in ``LOAD_DUMP`` after one of them has already replaced files
    under the Site Root (E003-S02-005 R1).
    """
    return any(e.event == "phase_started" and e.phase in _FILE_PHASES for e in entries)


def _site_untouched(header: JournalHeader, *, files_changed: bool) -> str:
    if not files_changed:
        return "live site untouched"
    return (
        "the live database is untouched, but files were replaced by this Run: "
        f"{_snapshot_undo(header)}"
    )


def _load_undo(step: str | None, header: JournalHeader, *, files_changed: bool) -> str:
    """The ``LOAD_DUMP`` rows of the undo table (ADR-0020)."""
    load, before = header.load_database, header.live_database_before
    keys = header.cutover_keys
    untouched = _site_untouched(header, files_changed=files_changed)
    if step is None or step in {STEP_CHECK_FAILED, STEP_CUTOVER_READBACK_FAILED}:
        if header.staging_database is None:
            named = f"{load} existed and was empty before this Run; empty or drop it"
            return f"{untouched}; {named}" if files_changed else named
        return f"{untouched}; drop staging database {load} if it exists"
    if step == STEP_STAGING_DROPPED:
        return f"{untouched}; the staging database was already dropped"
    if step.startswith(STEP_CUTOVER_STARTED):
        key = step.removeprefix(STEP_CUTOVER_STARTED)
        return (
            f"the cutover was writing {key} and it may have landed; do not drop "
            f"{load}; run `--resolve-interrupted-run {header.run_id}` to complete "
            "the set (it re-reads which keys landed), or set the keys written so "
            f"far back to {_prior_values(header)}"
        )
    if step.startswith(STEP_CUTOVER):
        key = step.removeprefix(STEP_CUTOVER)
        if keys and key != keys[-1]:
            written = keys[: keys.index(key) + 1] if key in keys else (key,)
            return (
                f"set {', '.join(written)} back to {_prior_values(header)}, or run "
                f"`--resolve-interrupted-run {header.run_id}` to complete the set"
            )
        return (
            f"the site now runs on {load}; the previous database {before} is "
            "retained; to go back, restore the database from Safety Snapshot "
            f"{header.snapshot_path or '(none: opted out)'}"
        )
    if step == STEP_LOSER_DROP_STARTED:
        return (
            f"the site runs on {load}; removal of the previous database {before} "
            "was in progress and it may be partly or fully dropped; to go back, "
            "restore the database from Safety Snapshot "
            f"{header.snapshot_path or '(none: opted out)'}"
        )
    if step == STEP_LOSER_DROPPED:
        return f"the site runs on {load}; {before} was dropped, as this Mode names"
    # A step this table does not know: never name a database to drop.
    return f"unrecognised step {step!r} in LOAD_DUMP; inspect {header.wp_config_path}"


def undo_for(
    interrupted: RestorePhase | None,
    step: str | None,
    header: JournalHeader,
    *,
    files_changed: bool,
) -> str:
    """What would undo a Run interrupted in ``interrupted`` after ``step``.

    The one derivation of ADR-0020's undo table, from the journal header,
    the last step and whether a file phase started (:func:`replaced_files`);
    nothing is stored. ``files_changed`` changes only the ``LOAD_DUMP`` rows
    that would otherwise say the live site is untouched. From the first
    ``"cutover_started:*"`` step on, the text never names
    ``header.load_database`` as something to drop.
    """
    config = header.wp_config_path
    match interrupted:
        case (
            None | RestorePhase.PREFLIGHT | RestorePhase.VERIFY | RestorePhase.SNAPSHOT
        ):
            return f"nothing on the Site was changed; {_resolve_hint(header)}"
        case RestorePhase.CLEAN:
            return _snapshot_undo(header)
        case RestorePhase.EXTRACT:
            if step == STEP_CONFIG_STARTED:
                return (
                    f"remove {config} if present (this Run was laying it down; "
                    f"none existed), then {_snapshot_undo(header)}"
                )
            if step == STEP_CONFIG:
                return (
                    f"remove {config} (laid down by this Run; none existed), then "
                    f"{_snapshot_undo(header)}"
                )
            return _snapshot_undo(header)
        case RestorePhase.REWRITE_CONFIG:
            # No saved copy (``KEEP``): the relocation's own undo text, never
            # "copy None back".
            saved = header.saved_config
            if step is not None and step.startswith("config_") and saved is not None:
                return f"copy {saved} back to {config}"
            return header.phase_undo.get(interrupted) or f"{config} unchanged"
        case RestorePhase.SEARCH_REPLACE:
            return header.phase_undo.get(interrupted) or f"{config} unchanged"
        case RestorePhase.LOAD_DUMP:
            return _load_undo(step, header, files_changed=files_changed)
        case _:
            assert_never(interrupted)


class IncompleteRunError(RefusedError):
    """AD-18: an earlier Run on this Site never finished, and nothing was resolved.

    ``interrupted_phase`` and ``undo`` are derived when the journal is read
    (:func:`interrupted_phase`, :func:`undo_for`); ``written_keys`` and
    ``pending_keys`` are set when the Run stopped inside the cutover.
    Names, paths and the Run ID only — the journal holds no credential.
    """

    def __init__(
        self,
        *,
        run_id: str,
        started_at: str,
        interrupted_phase: RestorePhase | None,
        undo: str,
        journal_path: str,
        written_keys: tuple[str, ...] = (),
        pending_keys: tuple[str, ...] = (),
    ) -> None:
        self.run_id = run_id
        self.started_at = started_at
        self.interrupted_phase = interrupted_phase
        self.undo = undo
        self.journal_path = journal_path
        self.written_keys = written_keys
        self.pending_keys = pending_keys
        phase = "no phase" if interrupted_phase is None else interrupted_phase.value
        keys = ""
        if written_keys or pending_keys:
            keys = (
                f"; cutover keys written: {', '.join(written_keys) or 'none'}, "
                f"pending: {', '.join(pending_keys) or 'none'}"
            )
        super().__init__(
            f"run {run_id} (started {started_at}) was interrupted in {phase}{keys}; "
            f"undo: {undo}; journal: {journal_path}; acknowledge it with "
            f"--resolve-interrupted-run {run_id}"
        )


def incomplete_run_error(
    record: JournalRecord, journal_path: str
) -> IncompleteRunError:
    """The refusal a later Run raises for the incomplete ``record``."""
    header, entries = record.header, record.entries
    phase = interrupted_phase(entries)
    step = last_step(entries, phase)
    written: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()
    if (
        phase is RestorePhase.LOAD_DUMP
        and step is not None
        and (step.startswith((STEP_CUTOVER, STEP_CUTOVER_STARTED)))
    ):
        written, pending = cutover_progress(entries, header)
    return IncompleteRunError(
        run_id=header.run_id,
        started_at=header.started_at,
        interrupted_phase=phase,
        undo=undo_for(phase, step, header, files_changed=replaced_files(entries)),
        journal_path=journal_path,
        written_keys=written,
        pending_keys=pending,
    )
