"""ADR-0020, ADR-0021 — a restore's journal steps, read back.

Split from ``core/restore.py`` (BL-E003-109). The named journal steps every
story that journals or reads a step uses (``STEP_*``);
:func:`is_orderly_failure`, whether a failed Run left the Site as it was;
and the read-back of an interrupted Run's journal: the phase it stopped in
(:func:`interrupted_phase`), its last step, the cutover's progress, the undo
text an operator is given (:func:`undo_for`) and the refusal that names it
(:class:`IncompleteRunError`).

**The direct load (owner decisions 49 to 55, ADR-0027).** ``LOAD_DUMP``
loads straight into the live database after the Safety Snapshot, journaling
``"database_created"`` (an administrator created it), ``"load_started"``,
``"loaded"`` (loaded and checked), then, on a failure after the load began,
``"rollback_started"`` and ``"rolled_back"`` or ``"rollback_failed"``; a
relocated database this Run created and dropped again is
``"load_target_dropped"``. The staging steps of the earlier design
(``"check_failed"``, ``"cutover_readback_failed"``, ``"staging_dropped"``,
``"loser_*"``) are never written now, but a journal an earlier version left
is read with them: the on-disk format is unchanged, so the undo table keeps
their rows.

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
    "STEP_DATABASE_CREATED",
    "STEP_LOADED",
    "STEP_LOAD_STARTED",
    "STEP_LOAD_TARGET_DROPPED",
    "STEP_LOSER_DROPPED",
    "STEP_LOSER_DROP_STARTED",
    "STEP_ROLLBACK_FAILED",
    "STEP_ROLLBACK_STARTED",
    "STEP_ROLLED_BACK",
    "STEP_STAGING_DROPPED",
    "IncompleteRunError",
    "cutover_progress",
    "incomplete_run_error",
    "interrupted_phase",
    "is_orderly_failure",
    "last_step",
    "loads_live_database",
    "recovery_command",
    "replaced_files",
    "rollback_owed",
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
STEP_CUTOVER_READBACK_FAILED: Final = "cutover_readback_failed"
# The direct load (owner decisions 49, 50 and 55).
STEP_DATABASE_CREATED: Final = "database_created"
STEP_LOAD_STARTED: Final = "load_started"
STEP_LOADED: Final = "loaded"
STEP_ROLLBACK_STARTED: Final = "rollback_started"
STEP_ROLLED_BACK: Final = "rolled_back"
STEP_ROLLBACK_FAILED: Final = "rollback_failed"
STEP_LOAD_TARGET_DROPPED: Final = "load_target_dropped"
# The staging design's steps, read from a journal an earlier version wrote.
STEP_CHECK_FAILED: Final = "check_failed"
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
    started, no ``"cutover:*"`` step completed, and the database is as it
    was: the automatic rollback completed (``"rolled_back"``, owner decision
    50), or the relocated database this Run created was dropped again
    (``"load_target_dropped"``), or, in a journal of the staging design,
    ``"staging_dropped"`` with ``"check_failed"`` or
    ``"cutover_readback_failed"``. ``"rollback_failed"`` is never orderly:
    the Run is recorded interrupted, so the next Run refuses until it is
    resolved.
    """
    steps = {e.step for e in entries if e.event == "step_completed" and e.step}
    if any(e.event == "phase_started" and e.phase in _FILE_PHASES for e in entries):
        return False
    if any(step.startswith(STEP_CUTOVER) for step in steps):
        return False
    if STEP_ROLLBACK_FAILED in steps:
        return False
    if steps & {STEP_ROLLED_BACK, STEP_LOAD_TARGET_DROPPED}:
        return True
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


def loads_live_database(header: JournalHeader) -> bool:
    """Whether the Run loaded the live database itself (owner decision 49).

    A relocation to another database (``--new-db-name``) or server
    (``--new-db-host``) loads elsewhere; the live one is then untouched.
    """
    return (
        header.staging_database is None
        and header.load_database == header.live_database_before
        and "DB_HOST" not in header.cutover_targets
    )


def recovery_command(header: JournalHeader) -> str:
    """The commands that put the live database back from the Safety Snapshot.

    Resolving and restoring are never one command (E003-S02-006): the
    interrupted Run is acknowledged first, then the snapshot's database is
    restored. ``""`` when no snapshot was taken.
    """
    snapshot = header.snapshot_path
    if snapshot is None:
        return ""
    root = str(header.site_root)
    return (
        f"`wpbackup2 restore --wp-dir {root} --resolve-interrupted-run "
        f"{header.run_id}`, then `wpbackup2 restore --wp-dir {root} --archive "
        f"{snapshot} --mode db` (with this Run's passphrase if the snapshot is "
        "encrypted)"
    )


def rollback_owed(record: JournalRecord) -> bool:
    """Whether ``record``'s Run left the live database partly loaded.

    It stopped in ``LOAD_DUMP`` after ``"load_started"``, during its rollback,
    or after a failed one, and it loaded the live database itself. Resolving
    it acknowledges; restoring the Safety Snapshot is the operator's next
    command (owner decision 50).
    """
    phase = interrupted_phase(record.entries)
    step = last_step(record.entries, phase)
    owed = {STEP_LOAD_STARTED, STEP_ROLLBACK_STARTED, STEP_ROLLBACK_FAILED}
    return (
        phase is RestorePhase.LOAD_DUMP
        and step in owed
        and loads_live_database(record.header)
    )


def _from_snapshot(header: JournalHeader) -> str:
    if header.snapshot_path is None:
        return (
            "no Safety Snapshot was taken (opted out, or the database was "
            "empty): restore the database from another backup, or empty it"
        )
    return (
        f"restore it from Safety Snapshot {header.snapshot_path}: "
        f"{recovery_command(header)}"
    )


def _direct_undo(step: str, header: JournalHeader, *, files_changed: bool) -> str:
    """The direct load's rows of the undo table (owner decisions 49 and 50)."""
    load = header.load_database
    untouched = _site_untouched(header, files_changed=files_changed)
    live = loads_live_database(header)
    if step == STEP_DATABASE_CREATED:
        return (
            f"{untouched}; this Run created database {load}, and nothing was "
            "loaded into it yet; drop it or keep it"
        )
    if step in {STEP_LOAD_STARTED, STEP_ROLLBACK_STARTED, STEP_ROLLBACK_FAILED}:
        what = {
            STEP_LOAD_STARTED: "the load was interrupted",
            STEP_ROLLBACK_STARTED: (
                "the load failed and its automatic rollback was interrupted"
            ),
            STEP_ROLLBACK_FAILED: "the load failed and its automatic rollback failed",
        }[step]
        if not live:
            return (
                f"{untouched}; {what}, so the relocated database {load} is "
                "partly loaded: empty or drop it"
            )
        return f"{what}, so the live database {load} is partly loaded; " + (
            _from_snapshot(header)
        )
    if step == STEP_LOADED:
        if header.cutover_keys:
            return (
                f"{untouched}; the Dump was loaded into {load} and checked, and "
                "the cutover had not started; empty or drop the relocated "
                "database, or restore again"
            )
        return (
            f"the Dump was loaded into {load} and checked: the database is "
            f"restored; {_resolve_hint(header)}"
        )
    if step == STEP_ROLLED_BACK:
        return (
            f"the load failed and the database {load} was put back from Safety "
            f"Snapshot {header.snapshot_path or '(none: it was empty)'}"
            + (f"; {_snapshot_undo(header)}" if files_changed else "")
        )
    # STEP_LOAD_TARGET_DROPPED
    return f"{untouched}; the relocated database {load} this Run created was dropped"


_DIRECT_STEPS: Final = frozenset(
    {
        STEP_DATABASE_CREATED,
        STEP_LOAD_STARTED,
        STEP_LOADED,
        STEP_ROLLBACK_STARTED,
        STEP_ROLLED_BACK,
        STEP_ROLLBACK_FAILED,
        STEP_LOAD_TARGET_DROPPED,
    }
)


def _load_undo(step: str | None, header: JournalHeader, *, files_changed: bool) -> str:
    """The ``LOAD_DUMP`` rows of the undo table (ADR-0020).

    The direct load's steps (:func:`_direct_undo`), the relocation cutover's,
    and the staging design's, which a journal an earlier version wrote may
    still hold (its header names a ``staging_database``).
    """
    load, before = header.load_database, header.live_database_before
    keys = header.cutover_keys
    untouched = _site_untouched(header, files_changed=files_changed)
    if step in _DIRECT_STEPS:
        return _direct_undo(step, header, files_changed=files_changed)
    if step is None and header.staging_database is None:
        return (
            f"{untouched}; nothing was loaded yet; if this Run created {load}, "
            "it is empty"
        )
    if step is None or step in {STEP_CHECK_FAILED, STEP_CUTOVER_READBACK_FAILED}:
        if header.staging_database is None:
            named = f"{load} was loaded by this Run; empty or drop it"
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
