"""ADR-0020/ADR-0021 — reading a journal back, purely (E003-S02-005).

``interrupted_phase``, ``last_step`` and ``undo_for`` are checked over every
prefix of entry sequences **recorded** from ``execute_restore`` with the real
``load_staged`` slot (``tests/staging_fakes.py``): an ``all-clean`` restore,
one with a four-key relocated cutover, and one laying down a config. The
scope is the recording, asserted to reach the cutover and the loser drop, so a
step added later is covered without editing a list here.
``is_orderly_failure``'s own tests are ``test_orderly_failure.py``'s.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path, PurePosixPath

import pytest

from l3io.wp.backup.adapters.staging import StagingCheckFailedError
from l3io.wp.backup.core import restore_journal as core_restore
from l3io.wp.backup.core.restore import MAX_DATABASE_NAME, NO_RELOCATION, staging_name
from l3io.wp.backup.core.restore_journal import (
    STEP_CHECK_FAILED,
    STEP_CONFIG_SAVED,
    STEP_CONFIG_STARTED,
    STEP_CUTOVER,
    STEP_CUTOVER_READBACK_FAILED,
    STEP_CUTOVER_STARTED,
    STEP_LOSER_DROP_STARTED,
    STEP_LOSER_DROPPED,
    STEP_STAGING_DROPPED,
    cutover_progress,
    incomplete_run_error,
    interrupted_phase,
    last_step,
    replaced_files,
    undo_for,
)
from l3io.wp.backup.domain.journal import JournalEntry, JournalHeader, JournalRecord
from l3io.wp.backup.domain.restore import RestoreMode, RestorePhase
from tests.conftest import CredentialSentinel
from tests.restore_harness import write_v1
from tests.staging_fakes import (
    FILE_PHASES,
    NOT_WORDPRESS,
    EntryJournal,
    StagedHarness,
    assert_one_row,
)

pytest.importorskip("l3io.wp.database")

#: Every step-name constant ``core/restore_journal.py`` declares (the source of
#: truth).
STEP_NAMES = sorted(
    value for name, value in vars(core_restore).items() if name.startswith("STEP_")
)
STAGING_DROP = "drop staging database"

SCENARIOS = {
    "all-clean": NO_RELOCATION,
    "relocated": dataclasses.replace(
        NO_RELOCATION,
        cutover_host="db.new",
        cutover_user="new_user",
        cutover_password=True,
    ),
    "lay-down": dataclasses.replace(NO_RELOCATION, lay_down_config=True),
}


def _record(
    tmp_path: Path,
    scenario: str,
    password: str,
    mode: RestoreMode = RestoreMode.ALL_CLEAN,
    *,
    check_fails: bool = False,
) -> tuple[JournalHeader, tuple[JournalEntry, ...]]:
    h = StagedHarness(
        work=tmp_path,
        archive_path=write_v1(tmp_path),
        mode=mode,
        relocation=SCENARIOS[scenario],
        password=password,
    )
    journal = EntryJournal()
    if check_fails:
        h.database.survey = NOT_WORDPRESS
        with pytest.raises(StagingCheckFailedError):
            h.run_staged(journal)
    else:
        h.run_staged(journal)
    assert journal.header is not None
    return journal.header, tuple(journal.entries)


@pytest.fixture(params=sorted(SCENARIOS))
def recorded(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    credential_sentinel: CredentialSentinel,
) -> tuple[str, JournalHeader, tuple[JournalEntry, ...]]:
    header, entries = _record(
        tmp_path, request.param, credential_sentinel.relocated_password
    )
    steps = {e.step for e in entries if e.step}
    assert any(s.startswith(STEP_CUTOVER_STARTED) for s in steps)
    assert {STEP_LOSER_DROP_STARTED, STEP_LOSER_DROPPED} <= steps
    if request.param == "lay-down":
        assert STEP_CONFIG_STARTED in steps
    return request.param, header, entries


def _oracle(prefix: tuple[JournalEntry, ...]) -> RestorePhase | None:
    """A state machine over the prefix: the open phase, else the last closed."""
    open_phase: RestorePhase | None = None
    closed: RestorePhase | None = None
    for entry in prefix:
        if entry.event == "phase_started":
            open_phase = entry.phase
        elif entry.event == "phase_completed" and entry.phase is open_phase:
            open_phase, closed = None, entry.phase
    return open_phase if open_phase is not None else closed


def _files_oracle(prefix: tuple[JournalEntry, ...]) -> bool:
    return any(e.event == "phase_started" and e.phase in FILE_PHASES for e in prefix)


def test_each_prefix_has_its_interrupted_phase_and_undo_row(
    recorded: tuple[str, JournalHeader, tuple[JournalEntry, ...]],
) -> None:
    _, header, entries = recorded
    cutover_begun = False
    for n in range(len(entries) + 1):
        prefix = entries[:n]
        phase = interrupted_phase(prefix)
        assert phase is _oracle(prefix)
        step = last_step(prefix, phase)
        files = _files_oracle(prefix)
        assert replaced_files(prefix) is files
        undo = undo_for(phase, step, header, files_changed=files)
        assert_one_row(undo, phase, step, header, files_changed=files)
        cutover_begun = cutover_begun or bool(
            step and step.startswith(STEP_CUTOVER_STARTED)
        )
        if cutover_begun:
            assert STAGING_DROP not in undo
            assert f"drop {header.load_database}" not in undo.replace("not drop", "")


def test_none_only_when_no_phase_started(
    recorded: tuple[str, JournalHeader, tuple[JournalEntry, ...]],
) -> None:
    _, _, entries = recorded
    assert interrupted_phase(()) is None
    first = next(i for i, e in enumerate(entries) if e.event == "phase_started")
    assert all(interrupted_phase(entries[:n]) is None for n in range(first + 1))
    assert all(
        interrupted_phase(entries[:n]) is not None
        for n in range(first + 1, len(entries))
    )


def test_written_and_pending_keys_inside_the_cutover(
    recorded: tuple[str, JournalHeader, tuple[JournalEntry, ...]],
) -> None:
    _, header, entries = recorded
    for n in range(len(entries) + 1):
        prefix = entries[:n]
        error = incomplete_run_error(JournalRecord(header, prefix), "/j/current.json")
        step = last_step(prefix, interrupted_phase(prefix))
        inside = error.interrupted_phase is RestorePhase.LOAD_DUMP and bool(
            step and step.startswith((STEP_CUTOVER, STEP_CUTOVER_STARTED))
        )
        if not inside:
            assert (error.written_keys, error.pending_keys) == ((), ())
            continue
        written, pending = cutover_progress(prefix, header)
        assert (error.written_keys, error.pending_keys) == (written, pending)
        assert written + pending == header.cutover_keys
        landed = [
            e.step.removeprefix(STEP_CUTOVER)
            for e in prefix
            if e.step and e.step.startswith(STEP_CUTOVER)
        ]
        assert list(written) == landed
        assert error.run_id in str(error)


def test_undo_for_is_total_over_every_phase_and_step(
    tmp_path: Path, credential_sentinel: CredentialSentinel
) -> None:
    header, _ = _record(tmp_path, "all-clean", credential_sentinel.relocated_password)
    assert STEP_NAMES
    for phase in [None, *RestorePhase]:
        for step in [None, *STEP_NAMES, STEP_CUTOVER_STARTED + "DB_NAME", "unknown"]:
            for files in (False, True):
                assert undo_for(phase, step, header, files_changed=files)


@pytest.mark.parametrize(
    ("mode", "files"),
    [(RestoreMode.ALL_CLEAN, True), (RestoreMode.DATABASE, False)],
    ids=["all-clean", "database"],
)
def test_a_failed_check_after_the_file_phases_names_the_files_undo(
    tmp_path: Path,
    credential_sentinel: CredentialSentinel,
    mode: RestoreMode,
    files: bool,
) -> None:
    """R1: CLEAN/EXTRACT ran before LOAD_DUMP, so the live site is not untouched."""
    header, entries = _record(
        tmp_path,
        "all-clean",
        credential_sentinel.relocated_password,
        mode,
        check_fails=True,
    )
    assert [e.step for e in entries if e.step] == [
        STEP_CHECK_FAILED,
        STEP_STAGING_DROPPED,
    ]
    error = incomplete_run_error(JournalRecord(header, entries), "/j/current.json")
    assert error.interrupted_phase is RestorePhase.LOAD_DUMP
    assert ("live site untouched" in error.undo) is not files
    assert ("files were replaced by this Run" in error.undo) is files
    for n in range(len(entries) + 1):
        prefix = entries[:n]
        phase = interrupted_phase(prefix)
        step = last_step(prefix, phase)
        undo = incomplete_run_error(JournalRecord(header, prefix), "/j").undo
        assert_one_row(undo, phase, step, header, files_changed=_files_oracle(prefix))


def test_staging_name_fits_and_is_distinct_per_run() -> None:
    wpdb = pytest.importorskip("l3io.wp.database")
    live = "w" * MAX_DATABASE_NAME
    names = {staging_name(live, run) for run in ("0a1b2c3d-x", "9f8e7d6c-y", "run-1")}
    assert len(names) == 3
    for name in names:
        assert len(name) <= MAX_DATABASE_NAME
        assert wpdb.validate_database_name(name) == name
    assert staging_name("wp", "1234-5678-90") == "wp_l3s_12345678"


# --------------------------------------------------------------------------
# The failure-path and fallback rows of the undo table (BL-E003-042, R5)
# --------------------------------------------------------------------------

_HEADER = JournalHeader(
    run_id="run-undo",
    started_at="2026-11-03T00:00:00+00:00",
    site_root=PurePosixPath("/srv/site"),
    restore_mode=RestoreMode.ALL,
    archive="/archives/a.tar.gz",
    snapshot_path="/snapshots/s.tar.gz",
    live_database_before="live_db",
    staging_database="live_db_staging",
    wp_config_path="/srv/site/wp-config.php",
    load_database="live_db_staging",
    cutover_keys=("DB_NAME",),
    cutover_targets={"DB_NAME": "live_db_staging"},
    prior_constants={"DB_NAME": "live_db"},
    phase_undo={},
    saved_config="/work/saved-wp-config.php",
)
#: The operator named the load database: no staging database was created.
_OPERATOR_NAMED = dataclasses.replace(
    _HEADER, staging_database=None, load_database="named_db"
)
_WITH_PHASE_UNDO = dataclasses.replace(
    _HEADER,
    phase_undo={
        RestorePhase.REWRITE_CONFIG: "planned rewrite undo",
        RestorePhase.SEARCH_REPLACE: "planned search-replace undo",
    },
)
#: ``KEEP``: no copy of the replaced ``wp-config.php`` was saved.
_NOTHING_SAVED = dataclasses.replace(_WITH_PHASE_UNDO, saved_config=None)
P_LOAD, P_REWRITE, P_SR = (
    RestorePhase.LOAD_DUMP,
    RestorePhase.REWRITE_CONFIG,
    RestorePhase.SEARCH_REPLACE,
)


@pytest.mark.parametrize(
    ("phase", "step", "header", "files", "wanted", "unwanted"),
    [
        (P_LOAD, STEP_CUTOVER_READBACK_FAILED, _HEADER, False,
         ("live site untouched",
          "drop staging database live_db_staging if it exists"), ()),
        (P_LOAD, None, _HEADER, False,
         ("drop staging database live_db_staging if it exists",), ()),
        (P_LOAD, STEP_CUTOVER_READBACK_FAILED, _HEADER, True,
         ("files were replaced by this Run", "drop staging database"),
         ("live site untouched",)),
        (P_LOAD, STEP_STAGING_DROPPED, _HEADER, False,
         ("live site untouched", "already dropped"), ("drop staging database",)),
        (P_LOAD, STEP_CHECK_FAILED, _OPERATOR_NAMED, False,
         ("named_db existed and was empty before this Run",),
         ("drop staging database",)),
        (P_LOAD, STEP_CUTOVER_READBACK_FAILED, _OPERATOR_NAMED, True,
         ("files were replaced by this Run", "named_db existed"),
         ("drop staging database",)),
        (P_LOAD, "no-such-step", _HEADER, False,
         ("unrecognised step 'no-such-step'", "/srv/site/wp-config.php"),
         ("drop",)),
        (P_REWRITE, STEP_CONFIG_SAVED, _HEADER, False,
         ("copy /work/saved-wp-config.php back to /srv/site/wp-config.php",), ()),
        (P_REWRITE, None, _HEADER, False,
         ("/srv/site/wp-config.php unchanged",), ()),
        (P_REWRITE, STEP_CONFIG_SAVED, _NOTHING_SAVED, False,
         ("planned rewrite undo",), ("None", "copy")),
        (P_REWRITE, None, _WITH_PHASE_UNDO, False, ("planned rewrite undo",), ()),
        (P_SR, None, _HEADER, False, ("/srv/site/wp-config.php unchanged",), ()),
        (P_SR, "anything", _WITH_PHASE_UNDO, False,
         ("planned search-replace undo",), ()),
    ],
)  # fmt: skip
def test_each_failure_and_fallback_row_says_what_undoes_it(
    phase: RestorePhase,
    step: str | None,
    header: JournalHeader,
    files: bool,
    wanted: tuple[str, ...],
    unwanted: tuple[str, ...],
) -> None:
    undo = undo_for(phase, step, header, files_changed=files)
    for text in wanted:
        assert text in undo, (text, undo)
    for text in unwanted:
        assert text not in undo, (text, undo)
