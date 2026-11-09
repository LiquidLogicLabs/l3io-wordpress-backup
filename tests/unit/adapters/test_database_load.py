"""Owner decisions 49, 50, 53, 55 — ``adapters/database_load.py``'s slots.

``load_live`` and ``check_live`` are driven through ``execute_restore``
(``tests/load_fakes.py``'s :class:`LoadHarness`) over a recording database
fake and ``tests/restore_harness.py``'s config fake, so one ordered timeline
holds every journal entry, database call and config write. The harness's
Safety Snapshot is a real Archive holding a Dump, so a rollback extracts and
reloads it as the real Run does.
"""

from __future__ import annotations

import dataclasses
import subprocess
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from l3io.wp.backup.adapters import database_load
from l3io.wp.backup.adapters.database import ADMIN_FLAGS, ADMIN_SOURCE
from l3io.wp.backup.adapters.database_load import (
    ADMINISTRATOR_RESOLVES,
    ConfigCutoverError,
    CutoverTargetUnusableError,
    InvalidLoadDatabaseNameError,
    LoadCheckFailedError,
    RollbackFailedError,
    check_live,
    complete_cutover,
    load_live,
)
from l3io.wp.backup.adapters.journal import CorruptJournalError
from l3io.wp.backup.cli.adapter_sets import dry_run_adapters
from l3io.wp.backup.cli.exit import exit_code_for
from l3io.wp.backup.core import restore as core_restore
from l3io.wp.backup.core.restore import CUTOVER_KEYS, NO_RELOCATION
from l3io.wp.backup.core.restore_journal import (
    STEP_CUTOVER,
    STEP_CUTOVER_READBACK_FAILED,
    STEP_CUTOVER_STARTED,
    STEP_DATABASE_CREATED,
    STEP_LOAD_STARTED,
    STEP_LOAD_TARGET_DROPPED,
    STEP_LOADED,
    STEP_ROLLBACK_FAILED,
    STEP_ROLLBACK_STARTED,
    STEP_ROLLED_BACK,
    is_orderly_failure,
)
from l3io.wp.backup.domain.config import ConfigWriteResult
from l3io.wp.backup.domain.database import (
    DatabaseLocation,
    DatabaseTargetState,
    Readiness,
)
from l3io.wp.backup.domain.errors import (
    AdminCredentialsRequiredError,
    CredentialsError,
    DatabaseRefusedError,
    DatabaseUnreachableError,
    LoadFailedError,
    ProvisioningFailedError,
    RunTerminated,
)
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.domain.journal import JournalEntry, JournalHeader, JournalRecord
from l3io.wp.backup.domain.restore import (
    RestoreMode,
    RestorePhase,
    SnapshotPolicy,
    restores_database,
)
from l3io.wp.backup.ports.adapter_set import AdapterSet
from l3io.wp.backup.ports.restore import RestorePorts
from tests.conftest import CredentialSentinel
from tests.load_fakes import (
    NEEDS_ADMIN,
    NOT_WORDPRESS,
    EntryJournal,
    LoadHarness,
)
from tests.restore_harness import TARGET_DB, CommandLog, FakeConfig, write_v1

pytest.importorskip("l3io.wp.database")

DATABASE_MODES = [m for m in RestoreMode if restores_database(m)]
#: A relocation to another database on another server (``--new-db-*``).
ELSEWHERE = DatabaseLocation(host="db.new", port=3307, name="ops_db")
#: The live database, as the harness's Site names it.
LIVE = "target_db"


def _harness(tmp_path: Path, **kwargs: object) -> tuple[LoadHarness, EntryJournal]:
    archive = write_v1(tmp_path)
    kwargs.setdefault("mode", RestoreMode.DATABASE)
    h = LoadHarness(work=tmp_path, archive_path=archive, **kwargs)  # type: ignore[arg-type]
    journal = EntryJournal(h.log)
    return h, journal


def _relocated(**fields: object) -> core_restore.RestoreRelocation:
    return dataclasses.replace(NO_RELOCATION, **fields)  # type: ignore[arg-type]


#: A relocation that moves the site to another server, database and account:
#: all four keys are cut over, and the live database is never loaded.
MOVED = _relocated(
    load_database=ELSEWHERE.name,
    load_location=ELSEWHERE,
    cutover_host=f"{ELSEWHERE.host}:{ELSEWHERE.port}",
    cutover_user="new_user",
    cutover_password=True,
)
#: A relocation of the account only: the live database is loaded as the new
#: account, then ``DB_USER`` and ``DB_PASSWORD`` are cut over.
NEW_ACCOUNT = _relocated(cutover_user="new_user", cutover_password=True)


def _timeline(h: LoadHarness) -> list[str]:
    return [f"{port}.{method}:{detail}" for _, port, method, detail in h.log.calls]


def _loads(h: LoadHarness) -> list[str]:
    return [name for _, method, name in h.database.calls if method == "load_dump"]


def _reports(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in logs if e.get("event") == "database_restore_report"]


# --------------------------------------------------------------------------
# Decision 49/53: the Dump loads straight into the live database
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", DATABASE_MODES)
def test_a_restore_loads_the_live_database_over_its_prefixed_tables(
    tmp_path: Path, mode: RestoreMode
) -> None:
    h, journal = _harness(tmp_path, mode=mode)
    with capture_logs() as logs:
        report = h.run_load(journal)
    assert report.database is not None
    assert _loads(h) == [LIVE]
    ((source, replace, prefix),) = h.database.loads
    assert (replace, prefix) == (True, "wp_")
    assert source.name == "database.sql"
    assert journal.steps() == [STEP_LOAD_STARTED, STEP_LOADED]
    assert not [c for c in h.log.calls if c[1] == "config"]
    assert "ensure_database" not in h.database.methods()
    assert report.database.restored is True
    assert report.database.administrator is False
    assert report.database.dropped_tables == h.database.dropped
    assert report.database.live_database_after == LIVE
    assert report.database.database == f"db.target:3306/{LIVE}"
    (dropped,) = [e for e in logs if e["event"] == "database_tables_dropped"]
    assert dropped["tables"] == list(h.database.dropped)
    assert dropped["table_prefix"] == "wp_"
    assert journal.finished


def test_the_post_load_check_surveys_the_loaded_database_before_loaded(
    tmp_path: Path,
) -> None:
    h, journal = _harness(tmp_path)
    h.run_load(journal)
    timeline = _timeline(h)
    loaded = timeline.index(f"journal.step_completed:{STEP_LOADED}")
    surveyed = timeline.index(f"database.survey_target:{LIVE}")
    started = timeline.index(f"journal.step_completed:{STEP_LOAD_STARTED}")
    assert started < timeline.index(f"database.load_dump:{LIVE}") < surveyed < loaded


def test_a_not_performed_load_surveys_nothing_and_reports_the_planned_drops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h, journal = _harness(tmp_path)
    h.database.performed = False
    h.database.survey = NOT_WORDPRESS  # would fail, were it read
    planned = ("wp_options", "wp_plugin_extra")
    real_inputs = h.inputs
    monkeypatch.setattr(
        h,
        "inputs",
        lambda: dataclasses.replace(real_inputs(), prefixed_tables=planned),
    )
    report = h.run_load(journal)
    assert "survey_target" not in h.database.methods()
    assert report.database is not None
    assert report.database.performed is False
    assert report.database.restored is False
    assert report.database.dropped_tables == planned


# --------------------------------------------------------------------------
# Decision 50: a failure after the load began is rolled back
# --------------------------------------------------------------------------


def test_a_failed_post_load_check_reloads_the_safety_snapshot(tmp_path: Path) -> None:
    h, journal = _harness(tmp_path)
    h.database.survey = NOT_WORDPRESS
    with capture_logs() as logs, pytest.raises(LoadCheckFailedError):
        h.run_load(journal)
    assert journal.steps() == [
        STEP_LOAD_STARTED,
        STEP_ROLLBACK_STARTED,
        STEP_ROLLED_BACK,
    ]
    assert is_orderly_failure(tuple(journal.entries))
    assert journal.finished
    assert _loads(h) == [LIVE, LIVE]
    first, rollback = h.database.loads
    assert rollback[1:] == (True, "wp_")
    assert rollback[0].parent == h.workspace / "rollback"
    assert first[0] != rollback[0]
    (rolled,) = [e for e in logs if e["event"] == "database_rolled_back"]
    assert rolled["snapshot"] == str(h.snapshot_file)
    assert rolled["log_level"] == "warning"


@pytest.mark.parametrize(
    "error",
    [
        LoadFailedError("the load exited 1", database=LIVE, exit_status=1),
        DatabaseRefusedError(findings=("cross_family_dump",)),
    ],
    ids=["load-failed", "refused"],
)
def test_a_failed_load_is_rolled_back_and_its_own_error_raised(
    tmp_path: Path, error: BaseException
) -> None:
    h, journal = _harness(tmp_path)
    h.database.failures["load_dump"] = error
    h.database.fail_loads = frozenset({1})
    with pytest.raises(type(error)) as caught:
        h.run_load(journal)
    assert caught.value is error
    assert journal.steps() == [
        STEP_LOAD_STARTED,
        STEP_ROLLBACK_STARTED,
        STEP_ROLLED_BACK,
    ]
    assert is_orderly_failure(tuple(journal.entries))
    assert journal.finished


def test_a_failed_rollback_names_both_failures_the_snapshot_and_the_command(
    tmp_path: Path, credential_sentinel: CredentialSentinel
) -> None:
    h, journal = _harness(tmp_path)
    h.database.failures["load_dump"] = LoadFailedError(
        "the load exited 1", database=LIVE, exit_status=1
    )
    with capture_logs() as logs, pytest.raises(RollbackFailedError) as caught:
        h.run_load(journal)
    error = caught.value
    credential_sentinel.add_exception("RollbackFailedError", error)
    credential_sentinel.assert_clean()
    assert isinstance(error.cause, LoadFailedError)
    assert isinstance(error.rollback, LoadFailedError)
    assert error.snapshot == str(h.snapshot_file)
    text = str(error)
    assert "LoadFailedError" in text
    assert "rolling it back from the Safety Snapshot failed too" in text
    assert str(h.snapshot_file) in text
    assert f"--resolve-interrupted-run {h.header().run_id}" in text
    assert f"--archive {h.snapshot_file} --mode db" in text
    assert "recorded as interrupted" in text
    assert exit_code_for(error) is ExitCode.FAILED
    assert journal.steps() == [
        STEP_LOAD_STARTED,
        STEP_ROLLBACK_STARTED,
        STEP_ROLLBACK_FAILED,
    ]
    assert not is_orderly_failure(tuple(journal.entries))
    assert not journal.finished
    (failed,) = [e for e in logs if e["event"] == "database_rollback_failed"]
    assert (failed["cause"], failed["rollback_error"]) == (
        "LoadFailedError",
        "LoadFailedError",
    )


def test_without_a_safety_snapshot_nothing_can_roll_back(tmp_path: Path) -> None:
    h, journal = _harness(tmp_path, snapshot=SnapshotPolicy.SKIP)
    h.database.survey = NOT_WORDPRESS
    with pytest.raises(RollbackFailedError) as caught:
        h.run_load(journal)
    assert caught.value.rollback is None
    assert "no Safety Snapshot was taken" in str(caught.value)
    assert isinstance(caught.value.cause, LoadCheckFailedError)
    assert journal.steps() == [STEP_LOAD_STARTED, STEP_ROLLBACK_FAILED]
    assert _loads(h) == [LIVE]
    assert not journal.finished


@pytest.mark.parametrize("created", [False, True])
def test_an_empty_live_database_is_emptied_again(tmp_path: Path, created: bool) -> None:
    """Decision 50 for a database that held nothing: no snapshot to reload."""
    h, journal = _harness(tmp_path, rollback="empty")
    h.database.survey = NOT_WORDPRESS
    if created:
        h.database.ensure_needed()
    with pytest.raises(LoadCheckFailedError):
        h.run_load(journal)
    expected = "drop_database" if created else "drop_tables"
    assert expected in h.database.methods("command")
    assert _loads(h) == [LIVE]
    assert journal.steps()[-2:] == [STEP_ROLLBACK_STARTED, STEP_ROLLED_BACK]
    assert is_orderly_failure(tuple(journal.entries))


@pytest.mark.parametrize(
    "interrupt", [RunTerminated("SIGTERM"), KeyboardInterrupt()], ids=["term", "int"]
)
def test_an_interrupted_load_is_not_rolled_back(
    tmp_path: Path, interrupt: BaseException
) -> None:
    h, journal = _harness(tmp_path)
    h.database.failures["load_dump"] = interrupt
    with pytest.raises(type(interrupt)):
        h.run_load(journal)
    assert journal.steps() == [STEP_LOAD_STARTED]
    assert _loads(h) == [LIVE]
    assert not journal.finished


def test_a_failure_before_the_load_began_rolls_nothing_back(tmp_path: Path) -> None:
    h, journal = _harness(tmp_path)
    h.database.ensure_needed()
    h.database.failures["ensure_database"] = ProvisioningFailedError("x")
    with pytest.raises(ProvisioningFailedError):
        h.run_load(journal)
    assert _loads(h) == []
    assert STEP_LOAD_STARTED not in journal.steps()


# --------------------------------------------------------------------------
# Decision 55: an administrator only when the account cannot load
# --------------------------------------------------------------------------


def test_an_administrator_is_used_only_when_the_account_cannot_load(
    tmp_path: Path,
) -> None:
    h, journal = _harness(tmp_path)
    h.database.ensure_needed()
    report = h.run_load(journal)
    assert report.database is not None
    assert report.database.administrator is True
    assert report.database.ensure.created_database is True
    assert h.database.methods("query").count("check_ensure") == 1
    assert h.database.methods("command")[:2] == ["ensure_database", "load_dump"]
    assert journal.steps()[:2] == [STEP_DATABASE_CREATED, STEP_LOAD_STARTED]


@pytest.mark.parametrize("dry", [False, True], ids=["real", "dry-run"])
@pytest.mark.parametrize("blocker", sorted(ADMINISTRATOR_RESOLVES))
def test_with_no_administrator_the_refusal_names_what_is_missing(
    tmp_path: Path, blocker: str, dry: bool
) -> None:
    h, _ = _harness(tmp_path)
    h.database.administrator_bound = False
    h.database.readiness["check_load"] = Readiness(ready=False, blockers=(blocker,))
    real = h.adapters()
    with pytest.raises(AdminCredentialsRequiredError) as caught:
        check_live(h.inputs(), ports=h.ports(dry_run_adapters(real) if dry else real))
    error = caught.value
    assert error.missing == (blocker,)
    assert error.database == LIVE
    assert error.account == TARGET_DB["DB_USER"]
    assert blocker in str(error)
    assert "--admin-username" in str(error)
    assert exit_code_for(error) is ExitCode.CREDENTIALS
    assert h.database.methods("command") == []


def test_a_ready_account_needs_no_administrator_even_when_none_is_bound(
    tmp_path: Path,
) -> None:
    h, journal = _harness(tmp_path)
    h.database.administrator_bound = False
    report = h.run_load(journal)
    assert report.database is not None
    assert report.database.restored is True
    assert "check_ensure" not in h.database.methods()


def test_a_blocker_no_administrator_resolves_refuses_naming_database_and_account(
    tmp_path: Path,
) -> None:
    h, _ = _harness(tmp_path)
    h.database.readiness["check_load"] = Readiness(
        ready=False, blockers=("database_absent", "cross_family_dump")
    )
    with pytest.raises(DatabaseRefusedError) as caught:
        check_live(h.inputs(), ports=h.ports())
    assert caught.value.findings == ("cross_family_dump",)
    assert caught.value.database == LIVE
    assert caught.value.account == TARGET_DB["DB_USER"]
    assert "check_ensure" not in h.database.methods()


def test_an_administrator_that_cannot_provision_refuses(tmp_path: Path) -> None:
    h, _ = _harness(tmp_path)
    h.database.ensure_needed()
    h.database.readiness["check_ensure"] = Readiness(
        ready=False, blockers=("missing_privilege",)
    )
    with pytest.raises(DatabaseRefusedError) as caught:
        check_live(h.inputs(), ports=h.ports())
    assert caught.value.account == ADMIN_SOURCE


def _check_calls(
    h: LoadHarness, *, dry: bool
) -> tuple[type[BaseException] | None, list[str]]:
    real = h.adapters()
    adapters = dry_run_adapters(real) if dry else real
    h.database.calls.clear()
    error: type[BaseException] | None = None
    try:
        check_live(h.inputs(), ports=h.ports(adapters))
    except Exception as exc:
        error = type(exc)
    return error, [f"{k}.{m}:{n}" for k, m, n in h.database.calls]


@pytest.mark.parametrize("readiness", ["ready", "needs-admin", "refused"])
@pytest.mark.parametrize("mode", DATABASE_MODES)
def test_check_live_refuses_alike_in_the_real_and_dry_run_sets(
    tmp_path: Path, mode: RestoreMode, readiness: str
) -> None:
    h, _ = _harness(tmp_path, mode=mode)
    if readiness == "needs-admin":
        h.database.ensure_needed()
    if readiness == "refused":
        h.database.readiness["check_load"] = Readiness(
            ready=False, blockers=("cross_family_dump",)
        )
    real_error, real_calls = _check_calls(h, dry=False)
    dry_error, dry_calls = _check_calls(h, dry=True)
    assert real_error is dry_error
    assert real_calls == dry_calls
    assert all(c.startswith("query.") for c in real_calls)
    assert real_error is (DatabaseRefusedError if readiness == "refused" else None)
    assert ("query.check_ensure:target_db" in real_calls) is (
        readiness == "needs-admin"
    )


# --------------------------------------------------------------------------
# A relocation (--new-db-*): the cutover, and the live database untouched
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("relocation", "keys", "load"),
    [
        (MOVED, CUTOVER_KEYS, ELSEWHERE.name),
        (NEW_ACCOUNT, ("DB_USER", "DB_PASSWORD"), LIVE),
    ],
    ids=["moved", "new-account"],
)
def test_a_relocation_cuts_over_each_key_inside_its_step_after_the_check(
    tmp_path: Path,
    credential_sentinel: CredentialSentinel,
    relocation: core_restore.RestoreRelocation,
    keys: tuple[str, ...],
    load: str,
) -> None:
    password = credential_sentinel.relocated_password
    h, journal = _harness(tmp_path, relocation=relocation, password=password)
    report = h.run_load(journal)
    credential_sentinel.add_repr("report", report)
    credential_sentinel.add_repr("journal entries", journal.entries)
    timeline = _timeline(h)
    writes = [i for i, c in enumerate(timeline) if c.startswith("config.set_constants")]
    surveyed = timeline.index(f"database.survey_target:{load}")
    assert len(writes) == len(keys)
    assert all(i > surveyed for i in writes)
    for i, key in zip(writes, keys, strict=True):
        assert timeline[i - 1] == f"journal.step_completed:{STEP_CUTOVER_STARTED}{key}"
        assert timeline[i + 1] == f"journal.step_completed:{STEP_CUTOVER}{key}"
    written = [
        d for _, port, m, d in h.log.calls if (port, m) == ("config", "set_constants")
    ]
    assert [list(w) for w in written] == [[k] for k in keys]
    assert {"DB_PASSWORD": password} in written
    assert _loads(h) == [load]
    assert journal.finished
    credential_sentinel.assert_clean()


def test_a_failure_loading_another_database_leaves_the_live_one_alone(
    tmp_path: Path, credential_sentinel: CredentialSentinel
) -> None:
    h, journal = _harness(
        tmp_path,
        relocation=MOVED,
        password=credential_sentinel.relocated_password,
    )
    h.database.ensure_needed()
    h.database.survey = NOT_WORDPRESS
    with pytest.raises(LoadCheckFailedError):
        h.run_load(journal)
    assert not [c for c in h.database.calls if c[2] == LIVE]
    assert ("command", "drop_database", ELSEWHERE.name) in h.database.calls
    assert journal.steps()[-1] == STEP_LOAD_TARGET_DROPPED
    assert is_orderly_failure(tuple(journal.entries))


def test_a_pre_existing_relocated_database_is_not_dropped_on_failure(
    tmp_path: Path, credential_sentinel: CredentialSentinel
) -> None:
    h, journal = _harness(
        tmp_path, relocation=MOVED, password=credential_sentinel.relocated_password
    )
    h.database.survey = NOT_WORDPRESS
    with pytest.raises(LoadCheckFailedError):
        h.run_load(journal)
    assert "drop_database" not in h.database.methods()
    assert journal.steps() == [STEP_LOAD_STARTED]
    assert not journal.finished


def test_a_read_back_failure_with_no_key_written_is_rolled_back(
    tmp_path: Path, credential_sentinel: CredentialSentinel
) -> None:
    h, journal = _harness(
        tmp_path,
        relocation=NEW_ACCOUNT,
        failing_keys=frozenset({"DB_USER"}),
        password=credential_sentinel.relocated_password,
    )
    with pytest.raises(ConfigCutoverError) as caught:
        h.run_load(journal)
    credential_sentinel.add_exception("ConfigCutoverError", caught.value)
    credential_sentinel.assert_clean()
    assert caught.value.written == ()
    assert journal.steps() == [
        STEP_LOAD_STARTED,
        STEP_LOADED,
        STEP_CUTOVER_STARTED + "DB_USER",
        STEP_CUTOVER_READBACK_FAILED,
        STEP_ROLLBACK_STARTED,
        STEP_ROLLED_BACK,
    ]
    assert is_orderly_failure(tuple(journal.entries))
    assert journal.finished
    assert h.config.values["DB_USER"] == TARGET_DB["DB_USER"]


def test_a_read_back_failure_after_a_written_key_stays_blocking(
    tmp_path: Path, credential_sentinel: CredentialSentinel
) -> None:
    h, journal = _harness(
        tmp_path,
        relocation=NEW_ACCOUNT,
        failing_keys=frozenset({"DB_PASSWORD"}),
        password=credential_sentinel.relocated_password,
    )
    with pytest.raises(ConfigCutoverError) as caught:
        h.run_load(journal)
    credential_sentinel.add_exception("ConfigCutoverError", caught.value)
    credential_sentinel.add_repr("journal entries", journal.entries)
    credential_sentinel.assert_clean()
    assert caught.value.written == ("DB_USER",)
    assert caught.value.pending == ("DB_PASSWORD",)
    assert not is_orderly_failure(tuple(journal.entries))
    assert not journal.finished
    assert _loads(h) == [LIVE]


@pytest.mark.parametrize("slot", ["check", "load"])
def test_an_invalid_load_name_is_refused_before_any_database_call(
    tmp_path: Path, slot: str
) -> None:
    h, journal = _harness(
        tmp_path, relocation=_relocated(load_database="bad/name`" + "x" * 70)
    )
    ports = h.ports()

    def call() -> object:
        if slot == "check":
            return check_live(h.inputs(), ports=ports)
        return load_live(h.inputs(), journal, ports=ports, workspace=h.workspace)

    with pytest.raises(InvalidLoadDatabaseNameError):
        call()
    assert h.database.calls == []


def test_a_relocated_password_without_a_source_refuses_before_any_command(
    tmp_path: Path,
) -> None:
    h, journal = _harness(tmp_path, relocation=_relocated(cutover_password=True))
    with pytest.raises(CredentialsError) as caught:
        h.run_load(journal)
    assert caught.value.missing_key == "DB_PASSWORD"
    assert h.database.methods("command") == []


def test_the_module_imports_without_the_database_extra() -> None:
    """AD-8: ``l3io.wp.database`` hidden from ``sys.modules`` — the import holds."""
    code = (
        "import sys; sys.modules['l3io.wp.database'] = None\n"
        "import l3io.wp.backup.adapters.database_load as s\n"
        "assert 'l3io.wp.database' not in {m for m in sys.modules if sys.modules[m]}\n"
        "print(sorted(s.ADMINISTRATOR_RESOLVES))\n"
    )
    done = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
    assert frozenset(eval(done.stdout)) == database_load.ADMINISTRATOR_RESOLVES  # noqa: S307 - our own repr


def test_the_module_never_imports_the_database_library_itself() -> None:
    """ADR-0015 (BL-E003-098): the name check goes through adapters/database."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(database_load))
    imported = [
        node.module if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    ]
    named = [
        f"{node.module}.{alias.name}"
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
        for alias in node.names
    ]
    assert not [m for m in imported + named if m and m.startswith("l3io.wp.database")]


# --------------------------------------------------------------------------
# ADV-S02-04: every cutover write error is classified
# --------------------------------------------------------------------------


class RaisingConfig(FakeConfig):
    """``set_constants`` of ``key`` raises ``OSError``, after landing if ``land``."""

    def __init__(
        self, log: CommandLog, *, key: str, land: bool, unreadable: bool
    ) -> None:
        super().__init__(log, TARGET_DB)
        self.key, self.land, self.unreadable = key, land, unreadable

    def set_constants(
        self, config_file: Path, values: Mapping[str, str | int | bool]
    ) -> ConfigWriteResult:
        if self.key not in values:
            return super().set_constants(config_file, values)
        if self.land:
            super().set_constants(config_file, values)
        msg = "the directory sync failed"
        raise OSError(msg)

    def read_constants(
        self, config_file: Path, keys: Sequence[str]
    ) -> Mapping[str, Any]:
        if self.unreadable:
            msg = "unreadable"
            raise OSError(msg)
        return super().read_constants(config_file, keys)


def _raising(
    tmp_path: Path,
    sentinel: CredentialSentinel,
    *,
    land: bool,
    unreadable: bool = False,
) -> tuple[LoadHarness, EntryJournal]:
    h, journal = _harness(
        tmp_path, relocation=NEW_ACCOUNT, password=sentinel.relocated_password
    )
    h.config = RaisingConfig(h.log, key="DB_USER", land=land, unreadable=unreadable)
    return h, journal


def test_an_os_error_on_the_first_key_that_did_not_land_is_rolled_back(
    tmp_path: Path, credential_sentinel: CredentialSentinel
) -> None:
    h, journal = _raising(tmp_path, credential_sentinel, land=False)
    with pytest.raises(ConfigCutoverError) as caught:
        h.run_load(journal)
    assert isinstance(caught.value.__cause__, OSError)
    assert caught.value.written == ()
    assert journal.steps()[-3:] == [
        STEP_CUTOVER_READBACK_FAILED,
        STEP_ROLLBACK_STARTED,
        STEP_ROLLED_BACK,
    ]
    assert is_orderly_failure(tuple(journal.entries))
    assert h.config.values["DB_USER"] == TARGET_DB["DB_USER"]


def test_an_os_error_after_the_write_landed_completes_the_cutover(
    tmp_path: Path, credential_sentinel: CredentialSentinel
) -> None:
    h, journal = _raising(tmp_path, credential_sentinel, land=True)
    report = h.run_load(journal)
    assert report.database is not None
    assert report.database.restored is True
    assert STEP_CUTOVER + "DB_USER" in journal.steps()
    assert journal.finished
    assert h.config.values["DB_USER"] == "new_user"


def test_an_os_error_that_cannot_be_read_back_stays_ambiguous(
    tmp_path: Path, credential_sentinel: CredentialSentinel
) -> None:
    """Unknown whether it landed: the intent stays last; nothing is rolled back."""
    h, journal = _raising(tmp_path, credential_sentinel, land=False, unreadable=True)
    with pytest.raises(OSError, match="directory sync"):
        h.run_load(journal)
    assert journal.steps()[-1] == STEP_CUTOVER_STARTED + "DB_USER"
    assert not journal.finished
    assert _loads(h) == [LIVE]


def test_a_value_that_cannot_be_had_journals_no_intent_for_its_key(
    tmp_path: Path,
    credential_sentinel: CredentialSentinel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The relocated password's source fails: its write never began."""
    h, journal = _harness(
        tmp_path, relocation=MOVED, password=credential_sentinel.relocated_password
    )
    real_ports = h.ports

    def failing_source() -> str:
        raise CredentialsError(database_load.CUTOVER_PASSWORD_SOURCE)

    def ports(adapters: AdapterSet | None = None) -> RestorePorts:
        bound = real_ports(adapters)
        return dataclasses.replace(bound, cutover_credentials=failing_source)

    monkeypatch.setattr(h, "ports", ports)
    with pytest.raises(CredentialsError):
        h.run_load(journal)
    steps = journal.steps()
    at = CUTOVER_KEYS.index("DB_PASSWORD")
    for key in CUTOVER_KEYS[:at]:
        assert STEP_CUTOVER + key in steps
    assert steps[-1] == STEP_CUTOVER + CUTOVER_KEYS[at - 1]
    assert STEP_CUTOVER_STARTED + "DB_PASSWORD" not in steps
    assert not journal.finished


# --------------------------------------------------------------------------
# FR-9: a failure raises, maps to its exit code and is never "restored"
# --------------------------------------------------------------------------

#: Each database failure and the exit code it maps to.
FAILURES: list[tuple[BaseException, ExitCode]] = [
    (DatabaseRefusedError(findings=("database_not_empty",)), ExitCode.REFUSED),
    (DatabaseUnreachableError("db.invalid", 3306), ExitCode.UNREACHABLE),
    (CredentialsError(ADMIN_SOURCE), ExitCode.CREDENTIALS),
    (
        AdminCredentialsRequiredError(ADMIN_SOURCE, flags=ADMIN_FLAGS),
        ExitCode.CREDENTIALS,
    ),
    (ProvisioningFailedError("ProvisioningError"), ExitCode.FAILED),
    (
        LoadFailedError("the load exited 1", database="wp", exit_status=1),
        ExitCode.FAILED,
    ),
]


@pytest.mark.parametrize("method", ["ensure_database", "load_dump"])
@pytest.mark.parametrize(
    ("error", "code"), FAILURES, ids=[type(e).__name__ for e, _ in FAILURES]
)
def test_each_database_failure_raises_maps_and_is_never_restored(
    tmp_path: Path, method: str, error: BaseException, code: ExitCode
) -> None:
    h, journal = _harness(tmp_path)
    h.database.ensure_needed()
    h.database.failures[method] = error
    h.database.fail_loads = frozenset({1})
    with capture_logs() as logs, pytest.raises(type(error)) as caught:
        h.run_load(journal)
    assert exit_code_for(caught.value) is code
    reports = _reports(logs)
    assert [r["log_level"] for r in reports] == ["error"]
    assert reports[0]["restored"] is False
    assert reports[0]["error_type"] == type(error).__name__
    assert ("created_database" in reports[0]) is (method == "load_dump")


def test_a_part_way_provisioning_failure_names_the_steps_that_ran(
    tmp_path: Path,
) -> None:
    h, journal = _harness(tmp_path)
    h.database.ensure_needed()
    error = ProvisioningFailedError("ProvisioningError", completed=("create_database",))
    h.database.failures["ensure_database"] = error
    with capture_logs() as logs, pytest.raises(ProvisioningFailedError):
        h.run_load(journal)
    (report,) = _reports(logs)
    assert report["completed"] == ["create_database"]


def test_a_success_logs_every_field_once_at_info(tmp_path: Path) -> None:
    h, journal = _harness(tmp_path)
    with capture_logs() as logs:
        outcome = h.run_load(journal)
    (report,) = _reports(logs)
    assert report["log_level"] == "info"
    assert report["restored"] is True
    assert outcome.database is not None
    assert report["dropped_tables"] == list(outcome.database.dropped_tables)
    assert report["administrator"] is False
    assert report["database"] == outcome.database.database


def test_a_part_way_cutover_names_the_keys_written_and_pending(
    tmp_path: Path, credential_sentinel: CredentialSentinel
) -> None:
    h, journal = _harness(
        tmp_path,
        relocation=NEW_ACCOUNT,
        failing_keys=frozenset({"DB_PASSWORD"}),
        password=credential_sentinel.relocated_password,
    )
    with capture_logs() as logs, pytest.raises(ConfigCutoverError):
        h.run_load(journal)
    (report,) = _reports(logs)
    assert report["cutover_completed"] is False
    assert (report["cutover_written"], report["cutover_pending"]) == (
        ["DB_USER"],
        ["DB_PASSWORD"],
    )


# --------------------------------------------------------------------------
# ADV-S02-05: completing an interrupted cutover surveys the load database first
# --------------------------------------------------------------------------

GONE = DatabaseTargetState(False, 0, False, None, None, 0, 0)
_AT = "2026-11-03T00:00:00+00:00"


def _record(
    h: LoadHarness, steps: Sequence[str], header: JournalHeader | None = None
) -> JournalRecord:
    phase = RestorePhase.LOAD_DUMP
    entries = (
        JournalEntry("phase_started", phase, _AT, LIVE, None),
        *(JournalEntry("step_completed", phase, _AT, None, step) for step in steps),
    )
    return JournalRecord(header or h.header(), entries)


def _complete(h: LoadHarness, record: JournalRecord) -> tuple[str, ...]:
    return complete_cutover(
        record, ports=h.ports(), table_prefix="wp_", journal_path="/j/current.json"
    )


def _writes(h: LoadHarness) -> list[object]:
    return [
        d for _, port, m, d in h.log.calls if (port, m) == ("config", "set_constants")
    ]


def _moved(tmp_path: Path, sentinel: CredentialSentinel) -> LoadHarness:
    h, _ = _harness(tmp_path, relocation=MOVED, password=sentinel.relocated_password)
    return h


def test_a_usable_load_database_completes_the_set(
    tmp_path: Path, credential_sentinel: CredentialSentinel
) -> None:
    h = _moved(tmp_path, credential_sentinel)
    record = _record(h, [STEP_LOADED, STEP_CUTOVER_STARTED + "DB_HOST"])
    assert _complete(h, record) == CUTOVER_KEYS
    assert ("query", "survey_target", ELSEWHERE.name) in h.database.calls
    assert h.config.values["DB_NAME"] == ELSEWHERE.name


@pytest.mark.parametrize("state", [GONE, NOT_WORDPRESS], ids=["gone", "not-wordpress"])
def test_an_unusable_load_database_is_refused_before_any_write(
    tmp_path: Path, credential_sentinel: CredentialSentinel, state: DatabaseTargetState
) -> None:
    h = _moved(tmp_path, credential_sentinel)
    h.database.survey = state
    h.config.values["DB_HOST"] = MOVED.cutover_host or ""  # the first key landed
    first = CUTOVER_KEYS[0]
    record = _record(
        h,
        [
            STEP_CUTOVER_STARTED + first,
            STEP_CUTOVER + first,
            STEP_CUTOVER_STARTED + "DB_USER",
        ],
    )
    with pytest.raises(CutoverTargetUnusableError) as caught:
        _complete(h, record)
    assert caught.value.exists is state.database_exists
    assert caught.value.exit_code is ExitCode.REFUSED
    assert _writes(h) == []


def test_a_gone_load_database_with_the_config_back_at_prior_writes_nothing(
    tmp_path: Path, credential_sentinel: CredentialSentinel
) -> None:
    h = _moved(tmp_path, credential_sentinel)
    h.database.survey = GONE
    record = _record(h, [STEP_CUTOVER_STARTED + "DB_HOST"])
    with capture_logs() as logs:
        assert _complete(h, record) == ()
    assert _writes(h) == []
    assert [e["event"] for e in logs if e["event"] == "cutover_abandoned"] == [
        "cutover_abandoned"
    ]


def test_a_header_without_a_pending_target_is_a_corrupt_journal(
    tmp_path: Path, credential_sentinel: CredentialSentinel
) -> None:
    h = _moved(tmp_path, credential_sentinel)
    header = dataclasses.replace(h.header(), cutover_targets={})
    record = _record(h, [STEP_CUTOVER_STARTED + "DB_HOST"], header)
    with pytest.raises(CorruptJournalError) as caught:
        _complete(h, record)
    assert caught.value.journal_path == "/j/current.json"
    assert _writes(h) == []
    assert h.database.calls == []


@pytest.mark.parametrize(
    "steps",
    [[STEP_LOAD_STARTED], [STEP_LOAD_STARTED, STEP_ROLLBACK_FAILED], [STEP_LOADED]],
)
def test_a_direct_load_interruption_has_no_cutover_to_complete(
    tmp_path: Path, steps: list[str]
) -> None:
    h, _ = _harness(tmp_path)
    assert _complete(h, _record(h, steps)) == ()
    assert _writes(h) == []
    assert h.database.calls == []


class _SteppingClock:
    """A ``Clock`` whose monotonic reading advances 7 ms per read."""

    def __init__(self) -> None:
        self.reads = 0

    def now(self) -> datetime:
        return datetime(2026, 11, 7, tzinfo=UTC)

    def monotonic_ns(self) -> int:
        self.reads += 1
        return self.reads * 7_000_000


def test_the_cutover_is_timed_by_the_runs_clock(
    tmp_path: Path, credential_sentinel: CredentialSentinel
) -> None:
    """BL-E003-100: the cutover's duration comes from the ``Clock`` port."""
    clock = _SteppingClock()
    h, journal = _harness(
        tmp_path,
        relocation=NEW_ACCOUNT,
        password=credential_sentinel.relocated_password,
        clock=clock,
    )
    with capture_logs() as logs:
        h.run_load(journal)
    (cutover,) = [
        e
        for e in logs
        if e["event"] == "phase_completed" and e.get("step") == "cutover"
    ]
    assert cutover["duration_ms"] == 7.0
    assert clock.reads == 2


def test_needs_admin_is_a_load_readiness_blocker() -> None:
    """The fake's provisioning answer is one only an administrator resolves."""
    assert set(NEEDS_ADMIN.blockers) <= ADMINISTRATOR_RESOLVES
