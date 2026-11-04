"""FR-9 — a database failure raises, maps to its exit code, and is never "restored".

E003-S02-004. ``tests/staging_fakes.py``'s :class:`FakeDatabase` raises each
of the package's database error classes in turn, from ``ensure_database`` and
from ``load_dump``. The executor raises (a failed load is never returned as a
value), ``cli/exit.py::exit_code_for`` gives the code the story's table
names, and the ``database_restore_report`` record is at ERROR with
``restored: false`` — never one saying ``restored: true``.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from l3io.wp.backup.adapters.database import ADMIN_FLAGS, ADMIN_SOURCE
from l3io.wp.backup.adapters.staging import ConfigCutoverError
from l3io.wp.backup.cli.adapter_sets import dry_run_adapters
from l3io.wp.backup.cli.exit import exit_code_for
from l3io.wp.backup.core.restore import staging_name
from l3io.wp.backup.domain.database import DatabaseLocation
from l3io.wp.backup.domain.errors import (
    AdminCredentialsRequiredError,
    CredentialsError,
    DatabaseRefusedError,
    DatabaseUnreachableError,
    LoadFailedError,
    ProvisioningFailedError,
)
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.domain.restore import RestoreMode, removes_database
from tests.restore_harness import write_v1
from tests.staging_fakes import RUN_ID, StagedHarness

pytest.importorskip("l3io.wp.database")

#: The story's table: each database failure and the exit code it maps to.
FAILURES: list[tuple[BaseException, ExitCode]] = [
    (DatabaseRefusedError(findings=("database_not_empty",)), ExitCode.REFUSED),
    (DatabaseUnreachableError("db.invalid", 3306), ExitCode.UNREACHABLE),
    (CredentialsError(ADMIN_SOURCE), ExitCode.CREDENTIALS),
    (
        AdminCredentialsRequiredError(ADMIN_SOURCE, flags=ADMIN_FLAGS),
        ExitCode.CREDENTIALS,
    ),
    (ProvisioningFailedError("ProvisioningError"), ExitCode.FAILED),
    (LoadFailedError("the load exited 1"), ExitCode.FAILED),
]
COMMANDS = ["ensure_database", "load_dump"]


def _reports(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in logs if e.get("event") == "database_restore_report"]


def _harness(tmp_path: Path) -> StagedHarness:
    return StagedHarness(
        work=tmp_path, archive_path=write_v1(tmp_path), mode=RestoreMode.DATABASE
    )


@pytest.mark.parametrize("method", COMMANDS)
@pytest.mark.parametrize(
    ("error", "code"), FAILURES, ids=[type(e).__name__ for e, _ in FAILURES]
)
def test_each_database_failure_raises_maps_and_is_never_restored(
    tmp_path: Path, method: str, error: BaseException, code: ExitCode
) -> None:
    h = _harness(tmp_path)
    h.database.failures[method] = error
    with capture_logs() as logs, pytest.raises(type(error)) as caught:
        h.run_staged()
    assert exit_code_for(caught.value) is code
    reports = _reports(logs)
    assert reports, "no database_restore_report was logged"
    assert all(r["restored"] is False for r in reports)
    assert [r["log_level"] for r in reports] == ["error"]
    assert reports[0]["error_type"] == type(error).__name__
    assert ("created_database" in reports[0]) is (method == "load_dump")


def test_a_part_way_provisioning_failure_names_the_steps_that_ran(
    tmp_path: Path,
) -> None:
    h = _harness(tmp_path)
    error = ProvisioningFailedError("ProvisioningError", completed=("create_database",))
    h.database.failures["ensure_database"] = error
    with capture_logs() as logs, pytest.raises(ProvisioningFailedError) as caught:
        h.run_staged()
    assert exit_code_for(caught.value) is ExitCode.FAILED
    assert "created database; then provisioning failed" in str(caught.value)
    (report,) = _reports(logs)
    assert report["completed"] == ["create_database"]
    assert report["restored"] is False


def test_a_refusal_names_its_finding_codes(tmp_path: Path) -> None:
    h = _harness(tmp_path)
    h.database.failures["load_dump"] = DatabaseRefusedError(
        findings=("cross_family_dump",)
    )
    with capture_logs() as logs, pytest.raises(DatabaseRefusedError):
        h.run_staged()
    (report,) = _reports(logs)
    assert report["findings"] == ["cross_family_dump"]


def test_a_success_logs_every_field_once_at_info(tmp_path: Path) -> None:
    h = _harness(tmp_path)
    with capture_logs() as logs:
        outcome = h.run_staged()
    (report,) = _reports(logs)
    assert report["log_level"] == "info"
    assert report["restored"] is True
    assert outcome.database is not None
    assert report["live_database_after"] == outcome.database.live_database_after
    assert report["loaded_bytes"] == outcome.database.loaded_bytes


# --------------------------------------------------------------------------
# The previous database, and progress on a failure after the cutover
# --------------------------------------------------------------------------

REMOVING = [m for m in RestoreMode if removes_database(m)]


def test_no_live_database_is_reported_as_none_not_retained(tmp_path: Path) -> None:
    h = _harness(tmp_path)
    h.live_database_exists = False
    surveyed = h.target
    h.target = lambda: dataclasses.replace(surveyed(), database=None)  # type: ignore[method-assign]
    outcome = h.run_staged()
    assert outcome.database is not None
    assert outcome.database.previous_database == "none"
    assert outcome.database.restored is True


@pytest.mark.parametrize("mode", REMOVING)
def test_a_drop_that_removed_nothing_is_none(
    tmp_path: Path, mode: RestoreMode, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = StagedHarness(work=tmp_path, archive_path=write_v1(tmp_path), mode=mode)
    real = h.database.drop_database

    def drop(location: DatabaseLocation, *, confirm: str) -> bool:
        real(location, confirm=confirm)
        return False

    monkeypatch.setattr(h.database, "drop_database", drop)
    outcome = h.run_staged()
    assert outcome.database is not None
    assert outcome.database.previous_database == "none"


@pytest.mark.parametrize("mode", REMOVING)
def test_a_dry_run_of_a_removing_mode_would_drop(
    tmp_path: Path, mode: RestoreMode
) -> None:
    h = StagedHarness(work=tmp_path, archive_path=write_v1(tmp_path), mode=mode)
    outcome = h.run_staged(adapters=dry_run_adapters(h.adapters()))
    assert outcome.database is not None
    assert outcome.database.previous_database == "dropped"
    assert outcome.database.restored is False


@pytest.mark.parametrize("mode", REMOVING)
def test_a_loser_failure_after_the_cutover_says_the_cutover_completed(
    tmp_path: Path, mode: RestoreMode
) -> None:
    h = StagedHarness(work=tmp_path, archive_path=write_v1(tmp_path), mode=mode)
    h.database.failures["drop_database"] = ProvisioningFailedError("DatabaseError")
    with capture_logs() as logs, pytest.raises(ProvisioningFailedError) as caught:
        h.run_staged()
    assert "nothing created" not in str(caught.value)
    (report,) = _reports(logs)
    assert report["restored"] is False
    assert report["cutover_completed"] is True
    assert report["cutover_written"] == ["DB_NAME"]
    assert report["live_database_after"] == staging_name(h.site.db_name, RUN_ID)


def test_a_part_way_cutover_names_the_keys_written_and_pending(
    tmp_path: Path,
) -> None:
    h = StagedHarness(
        work=tmp_path,
        archive_path=write_v1(tmp_path),
        mode=RestoreMode.DATABASE,
        failing_keys=frozenset({"DB_NAME"}),
    )
    with capture_logs() as logs, pytest.raises(ConfigCutoverError):
        h.run_staged()
    (report,) = _reports(logs)
    assert report["cutover_completed"] is False
    assert (report["cutover_written"], report["cutover_pending"]) == ([], ["DB_NAME"])
    assert "live_database_after" not in report
