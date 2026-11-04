"""FR-9, ADR-0022 — the database report says restored only when it was (E003-S02-004).

``core/restore.py::database_restored`` is a pure function of three facts — a
performed ``LoadResult``, the post-load check passed, the cutover written —
and ``adapters/staging.py::load_staged`` builds its report from it. A Dry Run
(``dry_run_adapters``) completes its plan with a not-performed, not-restored
report. ``ExecutionReport.database`` is ``None`` exactly for the Modes whose
plan has no ``LOAD_DUMP``. ``cli/restore.py`` renders the report and holds
the exit rule.
"""

from __future__ import annotations

import dataclasses
import itertools
import json
from pathlib import Path

import pytest

from l3io.wp.backup.cli.adapter_sets import dry_run_adapters
from l3io.wp.backup.cli.exit import exit_code_for
from l3io.wp.backup.cli.restore import (
    DatabaseNotRestoredError,
    database_report_json,
    database_report_lines,
    require_database_restored,
)
from l3io.wp.backup.core.restore import (
    DatabaseRestoreReport,
    database_report_fields,
    database_restored,
)
from l3io.wp.backup.domain.database import EnsureOutcome, LoadResult
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.domain.restore import RestoreMode, RestorePhase, restores_database
from tests.restore_harness import write_v1
from tests.staging_fakes import StagedHarness

pytest.importorskip("l3io.wp.database")

PERFORMED = LoadResult(bytes=10, duration_ms=1.0, performed=True)
NOT_PERFORMED = LoadResult(bytes=0, duration_ms=0.0, performed=False)


def _report(**changes: object) -> DatabaseRestoreReport:
    base = DatabaseRestoreReport(
        live_database_before="wp_live",
        live_database_after="wp_live_stage",
        ensure=EnsureOutcome(True, True, True, True, (), performed=True),
        loaded_bytes=2048,
        load_duration_ms=12.0,
        previous_database="retained",
        restored=True,
        performed=True,
    )
    return dataclasses.replace(base, **changes)  # type: ignore[arg-type]


def _harness(tmp_path: Path, mode: RestoreMode) -> StagedHarness:
    return StagedHarness(work=tmp_path, archive_path=write_v1(tmp_path), mode=mode)


# --------------------------------------------------------------------------
# The pure derivation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("performed", "check_passed", "cutover_written"),
    list(itertools.product([True, False], repeat=3)),
)
def test_restored_only_when_load_check_and_cutover_all_held(
    performed: bool, check_passed: bool, cutover_written: bool
) -> None:
    loaded = PERFORMED if performed else NOT_PERFORMED
    restored = database_restored(
        loaded, check_passed=check_passed, cutover_written=cutover_written
    )
    assert restored is (performed and check_passed and cutover_written)


def test_the_report_record_carries_every_report_and_ensure_field() -> None:
    """Derived from the two dataclasses, so a field added to either is checked."""
    fields = database_report_fields(_report())
    report = {f.name for f in dataclasses.fields(DatabaseRestoreReport)} - {"ensure"}
    ensure = {f.name for f in dataclasses.fields(EnsureOutcome)} - {"performed"}
    assert set(fields) == report | ensure


# --------------------------------------------------------------------------
# Through the executor: the Dry Run, and the report's presence per Mode
# --------------------------------------------------------------------------


def test_a_dry_run_completes_with_a_not_performed_not_restored_report(
    tmp_path: Path,
) -> None:
    h = _harness(tmp_path, RestoreMode.DATABASE)
    outcome = h.run_staged(adapters=dry_run_adapters(h.adapters()))
    assert outcome.phases == h.plan().phases
    database = outcome.database
    assert database is not None
    assert (database.performed, database.restored) == (False, False)
    assert database.ensure.performed is False
    assert not any((database.ensure.created_database, database.ensure.created_user))
    require_database_restored(h.plan(), outcome, dry_run=True)
    with pytest.raises(DatabaseNotRestoredError):
        require_database_restored(h.plan(), outcome, dry_run=False)


def test_a_real_run_reports_restored_from_its_results(tmp_path: Path) -> None:
    h = _harness(tmp_path, RestoreMode.DATABASE)
    outcome = h.run_staged()
    assert outcome.database is not None
    assert (outcome.database.performed, outcome.database.restored) == (True, True)
    require_database_restored(h.plan(), outcome, dry_run=False)


@pytest.mark.parametrize("mode", list(RestoreMode))
def test_the_report_is_absent_exactly_when_the_plan_has_no_load(
    tmp_path: Path, mode: RestoreMode
) -> None:
    h = _harness(tmp_path, mode)
    loads = RestorePhase.LOAD_DUMP in h.plan().phases
    assert loads is restores_database(mode)
    outcome = h.run_staged()
    assert (outcome.database is None) is (not loads)
    require_database_restored(h.plan(), outcome, dry_run=False)


# --------------------------------------------------------------------------
# cli/restore.py: the printed report and the exit rule
# --------------------------------------------------------------------------


def test_the_printed_report_names_what_ensure_did_and_says_restored() -> None:
    lines = database_report_lines(_report())
    text = "\n".join(lines)
    assert "the site now runs on 'wp_live_stage' (was 'wp_live')" in text
    assert "database: created 'wp_live_stage'" in text
    assert "WordPress user created" in text
    assert "loaded 2048 bytes" in text
    assert lines[-1] == "database: restored"


def test_an_account_that_existed_is_reported_as_existing() -> None:
    ensure = EnsureOutcome(True, False, False, True, (), performed=True)
    text = "\n".join(database_report_lines(_report(ensure=ensure)))
    assert "database: created" in text
    assert "WordPress user already existed" in text
    assert "password unchanged" in text


def test_password_unverified_is_printed() -> None:
    ensure = EnsureOutcome(
        True, False, False, True, ("password_unverified",), performed=True
    )
    text = "\n".join(database_report_lines(_report(ensure=ensure)))
    assert "warning password_unverified: " in text
    assert "was not verified" in text


def test_a_dry_run_report_says_would_and_never_created_or_restored() -> None:
    ensure = EnsureOutcome(False, False, False, False, ("x",), performed=False)
    report = _report(ensure=ensure, restored=False, performed=False, loaded_bytes=0)
    lines = database_report_lines(report)
    assert all("would" in line for line in lines[:3])
    assert lines[-1] == "database: not restored (Dry Run)"
    assert not [
        line for line in lines if "created" in line or line.endswith("restored")
    ]


def test_the_json_report_is_one_object_of_every_field() -> None:
    report = _report()
    decoded = json.loads(database_report_json(report))
    assert decoded == {"database_report": database_report_fields(report)}


@pytest.mark.parametrize(
    "report",
    [None, _report(restored=False), _report(restored=False, performed=False)],
    ids=["no-report", "not-restored", "not-performed"],
)
def test_a_real_run_with_a_load_never_exits_ok_unrestored(
    tmp_path: Path, report: DatabaseRestoreReport | None
) -> None:
    h = _harness(tmp_path, RestoreMode.DATABASE)
    outcome = dataclasses.replace(h.run_staged(), database=report)
    with pytest.raises(DatabaseNotRestoredError) as caught:
        require_database_restored(h.plan(), outcome, dry_run=False)
    assert exit_code_for(caught.value) is ExitCode.FAILED
