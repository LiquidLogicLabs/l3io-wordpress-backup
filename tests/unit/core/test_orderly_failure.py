"""ADR-0021 — which failed Runs leave the journal resolved (E003-S01-001)."""

from __future__ import annotations

import pytest

from l3io.wp.backup.core.restore_journal import (
    STEP_CHECK_FAILED,
    STEP_CONFIG,
    STEP_CONFIG_RESTORED,
    STEP_CONFIG_SAVED,
    STEP_CONFIG_STARTED,
    STEP_CUTOVER,
    STEP_CUTOVER_READBACK_FAILED,
    STEP_CUTOVER_STARTED,
    STEP_LOSER_DROP_STARTED,
    STEP_LOSER_DROPPED,
    STEP_RELOCATION_ROLLED_BACK,
    STEP_ROLLBACK_FAILED,
    STEP_ROLLED_BACK,
    STEP_STAGING_DROPPED,
    is_orderly_failure,
)
from l3io.wp.backup.domain.journal import JournalEntry
from l3io.wp.backup.domain.restore import RestorePhase

P = RestorePhase


def _started(phase: RestorePhase) -> JournalEntry:
    return JournalEntry("phase_started", phase, "t", "x", None)


def _step(phase: RestorePhase, step: str) -> JournalEntry:
    return JournalEntry("step_completed", phase, "t", None, step)


def _done(phase: RestorePhase) -> JournalEntry:
    return JournalEntry("phase_completed", phase, "t", None, None)


LOAD = _started(P.LOAD_DUMP)
CHECK_FAILED = (
    LOAD,
    _step(P.LOAD_DUMP, STEP_CHECK_FAILED),
    _step(P.LOAD_DUMP, STEP_STAGING_DROPPED),
)
READBACK_FAILED = (
    LOAD,
    _step(P.LOAD_DUMP, STEP_CUTOVER_STARTED + "DB_NAME"),
    _step(P.LOAD_DUMP, STEP_CUTOVER_READBACK_FAILED),
    _step(P.LOAD_DUMP, STEP_STAGING_DROPPED),
)
#: A hand-built ``all-clean`` sequence, in the order the real slot writes it.
ALL_CLEAN = (
    _started(P.SNAPSHOT),
    _done(P.SNAPSHOT),
    _started(P.CLEAN),
    _done(P.CLEAN),
    _started(P.EXTRACT),
    _step(P.EXTRACT, STEP_CONFIG_STARTED),
    _step(P.EXTRACT, STEP_CONFIG),
    _done(P.EXTRACT),
    LOAD,
    _step(P.LOAD_DUMP, STEP_CUTOVER_STARTED + "DB_NAME"),
    _step(P.LOAD_DUMP, STEP_CUTOVER + "DB_NAME"),
    _step(P.LOAD_DUMP, STEP_LOSER_DROP_STARTED),
    _step(P.LOAD_DUMP, STEP_LOSER_DROPPED),
    _done(P.LOAD_DUMP),
)


@pytest.mark.parametrize(
    "entries", [CHECK_FAILED, READBACK_FAILED], ids=["check", "readback"]
)
def test_the_two_orderly_sequences(entries: tuple[JournalEntry, ...]) -> None:
    assert is_orderly_failure(entries)
    snapshot_first = (_started(P.SNAPSHOT), _done(P.SNAPSHOT), *entries)
    assert is_orderly_failure(snapshot_first)


@pytest.mark.parametrize("cut", range(len(ALL_CLEAN) + 1))
def test_no_prefix_of_an_all_clean_run_is_orderly(cut: int) -> None:
    assert not is_orderly_failure(ALL_CLEAN[:cut])


@pytest.mark.parametrize("phase", [P.CLEAN, P.EXTRACT, P.REWRITE_CONFIG])
def test_a_started_file_phase_is_not_orderly(phase: RestorePhase) -> None:
    assert not is_orderly_failure((_started(phase), *CHECK_FAILED))


def test_a_completed_cutover_is_not_orderly() -> None:
    entries = (
        *READBACK_FAILED[:2],
        _step(P.LOAD_DUMP, STEP_CUTOVER + "DB_NAME"),
        *READBACK_FAILED[2:],
    )
    assert not is_orderly_failure(entries)


def test_no_staging_drop_is_not_orderly() -> None:
    assert not is_orderly_failure(CHECK_FAILED[:2])
    assert not is_orderly_failure(READBACK_FAILED[:3])


def test_a_cutover_intent_alone_does_not_make_it_non_orderly() -> None:
    assert READBACK_FAILED[1].step == "cutover_started:DB_NAME"
    assert is_orderly_failure(READBACK_FAILED)


#: Owner decision 72: a URL relocation's rewrite failed and was rolled back.
RELOCATION_UNDONE = (
    _started(P.REWRITE_CONFIG),
    _step(P.REWRITE_CONFIG, STEP_CONFIG_SAVED),
    _done(P.REWRITE_CONFIG),
    LOAD,
    _step(P.LOAD_DUMP, STEP_CUTOVER + "DB_NAME"),
    _done(P.LOAD_DUMP),
    _started(P.SEARCH_REPLACE),
    _step(P.LOAD_DUMP, STEP_ROLLED_BACK),
    _step(P.SEARCH_REPLACE, STEP_CONFIG_RESTORED),
    _step(P.SEARCH_REPLACE, STEP_RELOCATION_ROLLED_BACK),
)


def test_a_rolled_back_relocation_is_orderly_despite_its_config_writes() -> None:
    assert is_orderly_failure(RELOCATION_UNDONE)


@pytest.mark.parametrize("phase", [P.CLEAN, P.EXTRACT])
def test_a_rolled_back_relocation_after_file_phases_is_not_orderly(
    phase: RestorePhase,
) -> None:
    assert not is_orderly_failure((_started(phase), *RELOCATION_UNDONE))


def test_a_relocation_rollback_that_failed_is_not_orderly() -> None:
    failed = (*RELOCATION_UNDONE, _step(P.LOAD_DUMP, STEP_ROLLBACK_FAILED))
    assert not is_orderly_failure(failed)
