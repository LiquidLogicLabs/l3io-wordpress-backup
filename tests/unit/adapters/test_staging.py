"""AD-18.2/AD-18.3, ADR-0022 — ``adapters/staging.py``'s two slots (E003-S02-005).

``load_staged`` and ``check_staged`` are driven through ``execute_restore``
(``tests/staging_fakes.py``'s :class:`StagedHarness`) over a recording
database fake and ``tests/restore_harness.py``'s config fake, so one ordered
timeline holds every journal entry, database call and config write.
"""

from __future__ import annotations

import dataclasses
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from l3io.wp.backup.adapters import staging
from l3io.wp.backup.adapters.journal import CorruptJournalError
from l3io.wp.backup.adapters.staging import (
    ENSURE_RESOLVES,
    ConfigCutoverError,
    CutoverTargetUnusableError,
    InvalidLoadDatabaseNameError,
    StagingCheckFailedError,
    check_staged,
    complete_cutover,
    load_staged,
)
from l3io.wp.backup.cli.adapter_sets import dry_run_adapters
from l3io.wp.backup.core import restore as core_restore
from l3io.wp.backup.core.restore import (
    CUTOVER_KEYS,
    NO_RELOCATION,
    STEP_CHECK_FAILED,
    STEP_CUTOVER,
    STEP_CUTOVER_READBACK_FAILED,
    STEP_CUTOVER_STARTED,
    STEP_LOSER_DROP_STARTED,
    STEP_LOSER_DROPPED,
    STEP_STAGING_DROPPED,
    is_orderly_failure,
    staging_name,
)
from l3io.wp.backup.domain.config import ConfigWriteResult
from l3io.wp.backup.domain.database import (
    DatabaseLocation,
    DatabaseTargetState,
    Readiness,
)
from l3io.wp.backup.domain.errors import CredentialsError, DatabaseRefusedError
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.domain.journal import JournalEntry, JournalHeader, JournalRecord
from l3io.wp.backup.domain.restore import (
    RestoreMode,
    RestorePhase,
    removes_database,
    restores_database,
)
from l3io.wp.backup.ports.adapter_set import AdapterSet
from l3io.wp.backup.ports.restore import RestorePorts
from tests.conftest import CredentialSentinel
from tests.restore_harness import TARGET_DB, CommandLog, FakeConfig, write_v1
from tests.staging_fakes import NOT_WORDPRESS, EntryJournal, StagedHarness

pytest.importorskip("l3io.wp.database")

DATABASE_MODES = [m for m in RestoreMode if restores_database(m)]
OTHER_SERVER = DatabaseLocation(host="db.elsewhere", port=3307, name="ignored")


def _harness(tmp_path: Path, **kwargs: object) -> tuple[StagedHarness, EntryJournal]:
    archive = write_v1(tmp_path)
    h = StagedHarness(work=tmp_path, archive_path=archive, **kwargs)  # type: ignore[arg-type]
    journal = EntryJournal(h.log)
    return h, journal


def _relocated(**fields: object) -> core_restore.RestoreRelocation:
    return dataclasses.replace(NO_RELOCATION, **fields)  # type: ignore[arg-type]


def _timeline(h: StagedHarness) -> list[str]:
    return [f"{port}.{method}:{detail}" for _, port, method, detail in h.log.calls]


# --------------------------------------------------------------------------
# The cutover: only inside its step, after the post-load check
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("relocation", "keys"),
    [
        (NO_RELOCATION, ("DB_NAME",)),
        (
            _relocated(
                cutover_host="db.new", cutover_user="new_user", cutover_password=True
            ),
            CUTOVER_KEYS,
        ),
    ],
    ids=["db-name-only", "relocated-four-keys"],
)
def test_the_cutover_writes_each_key_inside_its_step_after_the_check(
    tmp_path: Path,
    credential_sentinel: CredentialSentinel,
    relocation: core_restore.RestoreRelocation,
    keys: tuple[str, ...],
) -> None:
    password = credential_sentinel.relocated_password
    h, journal = _harness(
        tmp_path, mode=RestoreMode.DATABASE, relocation=relocation, password=password
    )
    report = h.run_staged(journal)
    credential_sentinel.add_repr("report", report)
    credential_sentinel.add_repr("journal entries", journal.entries)

    timeline = _timeline(h)
    writes = [i for i, c in enumerate(timeline) if c.startswith("config.set_constants")]
    surveyed = timeline.index(
        f"database.survey_target:{report.database.live_database_after}"
    )  # type: ignore[union-attr]
    assert len(writes) == len(keys)
    assert all(i > surveyed for i in writes)
    for i, key in zip(writes, keys, strict=True):
        assert timeline[i - 1] == f"journal.step_completed:{STEP_CUTOVER_STARTED}{key}"
        assert timeline[i + 1] == f"journal.step_completed:{STEP_CUTOVER}{key}"
    written = [
        d for _, port, m, d in h.log.calls if (port, m) == ("config", "set_constants")
    ]
    assert [list(w) for w in written] == [[k] for k in keys]
    if "DB_PASSWORD" in keys:
        assert {"DB_PASSWORD": password} in written
    assert h.config.values["DB_NAME"] == staging_name("target_db", "run-1")
    assert journal.finished
    credential_sentinel.assert_clean()


def test_a_failed_post_load_check_is_an_orderly_failure(tmp_path: Path) -> None:
    h, journal = _harness(tmp_path, mode=RestoreMode.DATABASE)
    h.database.survey = NOT_WORDPRESS
    with pytest.raises(StagingCheckFailedError):
        h.run_staged(journal)
    assert journal.steps() == [STEP_CHECK_FAILED, STEP_STAGING_DROPPED]
    assert is_orderly_failure(tuple(journal.entries))
    assert journal.finished
    assert (
        "command",
        "drop_database",
        staging_name("target_db", "run-1"),
    ) in h.database.calls
    assert not [c for c in h.log.calls if c[1] == "config"]


class _DropNotJournaled(EntryJournal):
    """Fails to record ``staging_dropped``; every other write lands."""

    def step_completed(self, phase: RestorePhase, step: str) -> None:
        if step == STEP_STAGING_DROPPED:
            msg = "journal disk full"
            raise OSError(msg)
        super().step_completed(phase, step)


def test_a_journal_failure_after_the_drop_is_not_logged_as_a_failed_drop(
    tmp_path: Path,
) -> None:
    h, _ = _harness(tmp_path, mode=RestoreMode.DATABASE)
    journal = _DropNotJournaled(h.log)
    h.database.survey = NOT_WORDPRESS
    with capture_logs() as logs, pytest.raises(StagingCheckFailedError):
        h.run_staged(journal)
    events = [r["event"] for r in logs]
    assert "staging_database_drop_failed" not in events
    assert "staging_database_dropped" in events
    (record,) = [r for r in logs if r["event"] == "staging_dropped_not_journaled"]
    assert (record["log_level"], record["error_type"]) == ("error", "OSError")
    assert STEP_STAGING_DROPPED not in journal.steps()


def test_a_read_back_failure_with_no_key_written_is_orderly(
    tmp_path: Path, credential_sentinel: CredentialSentinel
) -> None:
    h, journal = _harness(
        tmp_path,
        mode=RestoreMode.DATABASE,
        failing_keys=frozenset({"DB_NAME"}),
        password=credential_sentinel.relocated_password,
    )
    with pytest.raises(ConfigCutoverError) as caught:
        h.run_staged(journal)
    credential_sentinel.add_exception("ConfigCutoverError", caught.value)
    credential_sentinel.assert_clean()
    assert caught.value.written == ()
    assert journal.steps() == [
        STEP_CUTOVER_STARTED + "DB_NAME",
        STEP_CUTOVER_READBACK_FAILED,
        STEP_STAGING_DROPPED,
    ]
    assert is_orderly_failure(tuple(journal.entries))
    assert journal.finished
    assert h.config.values["DB_NAME"] == "target_db"


@pytest.mark.parametrize("failing", ["DB_USER", "DB_PASSWORD"])
def test_a_read_back_failure_after_a_written_key_stays_blocking(
    tmp_path: Path, credential_sentinel: CredentialSentinel, failing: str
) -> None:
    h, journal = _harness(
        tmp_path,
        mode=RestoreMode.DATABASE,
        relocation=_relocated(
            cutover_host="db.new", cutover_user="new_user", cutover_password=True
        ),
        failing_keys=frozenset({failing}),
        password=credential_sentinel.relocated_password,
    )
    with pytest.raises(ConfigCutoverError) as caught:
        h.run_staged(journal)
    credential_sentinel.add_exception("ConfigCutoverError", caught.value)
    credential_sentinel.add_repr("journal entries", journal.entries)
    credential_sentinel.assert_clean()
    at = CUTOVER_KEYS.index(failing)
    assert caught.value.written == CUTOVER_KEYS[:at]
    assert caught.value.pending == CUTOVER_KEYS[at:]
    assert not is_orderly_failure(tuple(journal.entries))
    assert not journal.finished
    assert "drop_database" not in h.database.methods()


# --------------------------------------------------------------------------
# Names, the staging hook and the loser
# --------------------------------------------------------------------------


@pytest.mark.parametrize("slot", ["check", "load"])
def test_an_invalid_load_name_is_refused_before_any_database_call(
    tmp_path: Path, slot: str
) -> None:
    h, journal = _harness(
        tmp_path,
        mode=RestoreMode.DATABASE,
        relocation=_relocated(load_database="bad/name`" + "x" * 70),
    )
    ports = h.ports()

    def call() -> object:
        if slot == "check":
            return check_staged(h.inputs(), ports=ports)
        return load_staged(h.inputs(), journal, ports=ports, workspace=h.workspace)

    with pytest.raises(InvalidLoadDatabaseNameError):
        call()
    assert h.database.calls == []


def test_a_not_performed_load_surveys_nothing_and_does_not_fail(tmp_path: Path) -> None:
    h, journal = _harness(tmp_path, mode=RestoreMode.DATABASE)
    h.database.performed = False
    h.database.survey = NOT_WORDPRESS  # would fail, were it read
    report = h.run_staged(journal)
    assert "survey_target" not in h.database.methods()
    assert report.database is not None
    assert report.database.performed is False
    assert report.database.restored is False


def test_an_operator_named_database_is_addressed_without_a_staging_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h, journal = _harness(
        tmp_path,
        mode=RestoreMode.DATABASE,
        relocation=_relocated(load_database="ops_db"),
    )
    header = h.header()

    def refuse(*_: object) -> str:
        msg = "staging_name was called"
        raise AssertionError(msg)

    monkeypatch.setattr(core_restore, "staging_name", refuse)
    monkeypatch.setattr(h, "header", lambda: header)
    h.run_staged(journal)
    addressed = {
        name
        for _, m, name in h.database.calls
        if m
        in {
            "ensure_database",
            "load_dump",
            "survey_target",
            "check_ensure",
            "check_load",
        }
    }
    assert addressed == {"ops_db"}
    assert h.config.values["DB_NAME"] == "ops_db"


def test_a_pre_existing_operator_database_is_not_dropped_on_failure(
    tmp_path: Path,
) -> None:
    h, journal = _harness(
        tmp_path,
        mode=RestoreMode.DATABASE,
        relocation=_relocated(load_database="ops_db"),
    )
    h.database.created_database = False
    h.database.survey = NOT_WORDPRESS
    with pytest.raises(StagingCheckFailedError):
        h.run_staged(journal)
    assert "drop_database" not in h.database.methods()
    assert journal.steps() == [STEP_CHECK_FAILED]
    assert not journal.finished


@pytest.mark.parametrize("mode", [m for m in DATABASE_MODES if removes_database(m)])
def test_a_different_server_relocation_never_drops_the_old_location(
    tmp_path: Path, mode: RestoreMode
) -> None:
    h, journal = _harness(
        tmp_path, mode=mode, relocation=_relocated(load_location=OTHER_SERVER)
    )
    report = h.run_staged(journal)
    assert not [c for c in h.database.calls if c[2] == "target_db"]
    assert report.database is not None
    assert report.database.previous_database == "retained"


@pytest.mark.parametrize("mode", DATABASE_MODES)
def test_the_loser_is_dropped_only_by_a_mode_naming_removal(
    tmp_path: Path, mode: RestoreMode
) -> None:
    h, journal = _harness(tmp_path, mode=mode)
    report = h.run_staged(journal)
    assert report.database is not None
    live_calls = [
        m for k, m, n in h.database.calls if k == "command" and n == "target_db"
    ]
    if removes_database(mode):
        assert live_calls == ["revoke_grants", "drop_database"]
        assert journal.steps()[-2:] == [STEP_LOSER_DROP_STARTED, STEP_LOSER_DROPPED]
        assert report.database.previous_database == "dropped"
    else:
        assert live_calls == []
        assert report.database.previous_database == "retained"


def test_a_relocated_password_without_a_source_refuses_before_any_command(
    tmp_path: Path,
) -> None:
    h, journal = _harness(
        tmp_path,
        mode=RestoreMode.DATABASE,
        relocation=_relocated(cutover_password=True),
    )
    with pytest.raises(CredentialsError) as caught:
        h.run_staged(journal)
    assert caught.value.missing_key == "DB_PASSWORD"
    assert h.database.methods("command") == []


# --------------------------------------------------------------------------
# check_staged — the Dry Run parity test
# --------------------------------------------------------------------------

CHECKS = ["check_ensure", "check_load", "check_revoke", "check_drop_database"]


def _check_calls(
    h: StagedHarness, *, dry: bool
) -> tuple[type[BaseException] | None, list[str]]:
    real = h.adapters()
    adapters = dry_run_adapters(real) if dry else real
    h.database.calls.clear()
    error: type[BaseException] | None = None
    try:
        check_staged(h.inputs(), ports=h.ports(adapters))
    except Exception as exc:
        error = type(exc)
    return error, [f"{k}.{m}:{n}" for k, m, n in h.database.calls]


@pytest.mark.parametrize("blocked", [None, *CHECKS])
@pytest.mark.parametrize("mode", DATABASE_MODES)
def test_check_staged_refuses_alike_in_the_real_and_dry_run_sets(
    tmp_path: Path, mode: RestoreMode, blocked: str | None
) -> None:
    h, _ = _harness(tmp_path, mode=mode)
    if blocked is not None:
        h.database.readiness[blocked] = Readiness(
            ready=False, blockers=("missing_privilege",)
        )
    real_error, real_calls = _check_calls(h, dry=False)
    dry_error, dry_calls = _check_calls(h, dry=True)
    assert real_error is dry_error
    assert real_calls == dry_calls
    assert all(c.startswith("query.") for c in real_calls)
    loser_checks = {"check_revoke", "check_drop_database"}
    called = {c.split(":")[0].removeprefix("query.") for c in real_calls}
    assert (loser_checks <= called) is removes_database(mode)
    assert not (loser_checks & called) or removes_database(mode)
    reaches = blocked in called if blocked is not None else False
    assert real_error is (DatabaseRefusedError if reaches else None)


@pytest.mark.parametrize("mode", [m for m in DATABASE_MODES if removes_database(m)])
def test_check_staged_skips_the_loser_checks_on_another_server(
    tmp_path: Path, mode: RestoreMode
) -> None:
    h, _ = _harness(
        tmp_path, mode=mode, relocation=_relocated(load_location=OTHER_SERVER)
    )
    _, calls = _check_calls(h, dry=False)
    assert not [c for c in calls if "check_revoke" in c or "check_drop_database" in c]


@pytest.mark.parametrize("blocker", sorted(ENSURE_RESOLVES))
@pytest.mark.parametrize("ensure_ready", [True, False])
def test_ensure_resolves_blockers_refuse_only_when_ensure_is_not_ready(
    tmp_path: Path, blocker: str, ensure_ready: bool
) -> None:
    h, _ = _harness(tmp_path, mode=RestoreMode.DATABASE)
    h.database.readiness["check_ensure"] = Readiness(ready=ensure_ready)
    h.database.readiness["check_load"] = Readiness(ready=False, blockers=(blocker,))
    error, _ = _check_calls(h, dry=False)
    if ensure_ready:
        assert error is None
    else:
        expected = (
            CredentialsError
            if blocker == "authentication_failed"
            else DatabaseRefusedError
        )
        assert error is expected


def test_a_blocker_outside_ensure_resolves_refuses_even_when_ensure_is_ready(
    tmp_path: Path,
) -> None:
    h, _ = _harness(tmp_path, mode=RestoreMode.DATABASE)
    h.database.readiness["check_load"] = Readiness(
        ready=False, blockers=("database_absent", "database_not_empty")
    )
    error, _ = _check_calls(h, dry=False)
    assert error is DatabaseRefusedError


def test_staging_imports_without_the_database_extra() -> None:
    """AD-8: ``l3io.wp.database`` hidden from ``sys.modules`` — the import holds."""
    code = (
        "import sys; sys.modules['l3io.wp.database'] = None\n"
        "import l3io.wp.backup.adapters.staging as s\n"
        "assert 'l3io.wp.database' not in {m for m in sys.modules if sys.modules[m]}\n"
        "print(s.ENSURE_RESOLVES)\n"
    )
    done = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
    assert frozenset(eval(done.stdout)) == staging.ENSURE_RESOLVES  # noqa: S307 - our own repr


# --------------------------------------------------------------------------
# ADV-S02-04: every write error is classified; no intent for an unstarted write
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
    tmp_path: Path, *, land: bool, unreadable: bool = False
) -> tuple[StagedHarness, EntryJournal]:
    h, journal = _harness(tmp_path, mode=RestoreMode.DATABASE)
    h.config = RaisingConfig(h.log, key="DB_NAME", land=land, unreadable=unreadable)
    return h, journal


def test_an_os_error_on_the_first_key_that_did_not_land_is_orderly(
    tmp_path: Path,
) -> None:
    h, journal = _raising(tmp_path, land=False)
    with pytest.raises(ConfigCutoverError) as caught:
        h.run_staged(journal)
    assert isinstance(caught.value.__cause__, OSError)
    assert caught.value.written == ()
    assert journal.steps() == [
        STEP_CUTOVER_STARTED + "DB_NAME",
        STEP_CUTOVER_READBACK_FAILED,
        STEP_STAGING_DROPPED,
    ]
    assert is_orderly_failure(tuple(journal.entries))
    assert journal.finished
    assert h.config.values["DB_NAME"] == "target_db"


def test_an_os_error_after_the_write_landed_completes_the_cutover(
    tmp_path: Path,
) -> None:
    h, journal = _raising(tmp_path, land=True)
    report = h.run_staged(journal)
    assert report.database is not None
    assert report.database.restored is True
    assert STEP_CUTOVER + "DB_NAME" in journal.steps()
    assert journal.finished
    assert h.config.values["DB_NAME"] == staging_name("target_db", "run-1")


def test_an_os_error_that_cannot_be_read_back_stays_ambiguous(
    tmp_path: Path,
) -> None:
    """Unknown whether it landed: the intent stays the last step; nothing dropped."""
    h, journal = _raising(tmp_path, land=False, unreadable=True)
    with pytest.raises(OSError, match="directory sync"):
        h.run_staged(journal)
    assert journal.steps()[-1] == STEP_CUTOVER_STARTED + "DB_NAME"
    assert not journal.finished
    assert "drop_database" not in h.database.methods()


def test_a_value_that_cannot_be_had_journals_no_intent_for_its_key(
    tmp_path: Path,
    credential_sentinel: CredentialSentinel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The relocated password's source fails: its write never began."""
    h, journal = _harness(
        tmp_path,
        mode=RestoreMode.DATABASE,
        relocation=_relocated(
            cutover_host="db.new", cutover_user="new_user", cutover_password=True
        ),
        password=credential_sentinel.relocated_password,
    )
    real_ports = h.ports

    def failing_source() -> str:
        raise CredentialsError(staging.CUTOVER_PASSWORD_SOURCE)

    def ports(adapters: AdapterSet | None = None) -> RestorePorts:
        bound = real_ports(adapters)
        return dataclasses.replace(bound, cutover_credentials=failing_source)

    monkeypatch.setattr(h, "ports", ports)
    with pytest.raises(CredentialsError):
        h.run_staged(journal)
    steps = journal.steps()
    at = CUTOVER_KEYS.index("DB_PASSWORD")
    for key in CUTOVER_KEYS[:at]:
        assert STEP_CUTOVER + key in steps
    assert steps[-1] == STEP_CUTOVER + CUTOVER_KEYS[at - 1]
    assert STEP_CUTOVER_STARTED + "DB_PASSWORD" not in steps
    assert not journal.finished


# --------------------------------------------------------------------------
# ADV-S02-05: completing an interrupted cutover surveys the load database first
# --------------------------------------------------------------------------

GONE = DatabaseTargetState(False, 0, False, None, None, 0, 0)
_AT = "2026-11-03T00:00:00+00:00"


def _record(
    h: StagedHarness, steps: Sequence[str], header: JournalHeader | None = None
) -> JournalRecord:
    phase = RestorePhase.LOAD_DUMP
    entries = (
        JournalEntry("phase_started", phase, _AT, "target_db", None),
        *(JournalEntry("step_completed", phase, _AT, None, step) for step in steps),
    )
    return JournalRecord(header or h.header(), entries)


def _complete(h: StagedHarness, record: JournalRecord) -> tuple[str, ...]:
    return complete_cutover(
        record, ports=h.ports(), table_prefix="wp_", journal_path="/j/current.json"
    )


def _writes(h: StagedHarness) -> list[object]:
    return [
        d for _, port, m, d in h.log.calls if (port, m) == ("config", "set_constants")
    ]


def test_a_usable_load_database_completes_the_set(tmp_path: Path) -> None:
    h, _ = _harness(tmp_path, mode=RestoreMode.DATABASE)
    record = _record(h, [STEP_CUTOVER_STARTED + "DB_NAME"])
    assert _complete(h, record) == ("DB_NAME",)
    load = staging_name("target_db", "run-1")
    assert ("query", "survey_target", load) in h.database.calls
    assert h.config.values["DB_NAME"] == load


@pytest.mark.parametrize("state", [GONE, NOT_WORDPRESS], ids=["gone", "not-wordpress"])
def test_an_unusable_load_database_is_refused_before_any_write(
    tmp_path: Path, credential_sentinel: CredentialSentinel, state: DatabaseTargetState
) -> None:
    h, _ = _harness(
        tmp_path,
        mode=RestoreMode.DATABASE,
        relocation=_relocated(
            cutover_host="db.new", cutover_user="new_user", cutover_password=True
        ),
        password=credential_sentinel.relocated_password,
    )
    h.database.survey = state
    h.config.values["DB_HOST"] = "db.new"  # the first key landed
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
    surveyed = [c for c in h.database.calls if c[1] == "survey_target"]
    assert surveyed == [("query", "survey_target", record.header.load_database)]


def test_a_gone_load_database_with_the_config_back_at_prior_writes_nothing(
    tmp_path: Path,
) -> None:
    """Nothing to roll forward: the set is abandoned, so resolution can proceed."""
    h, _ = _harness(tmp_path, mode=RestoreMode.DATABASE)
    h.database.survey = GONE
    record = _record(h, [STEP_CUTOVER_STARTED + "DB_NAME"])
    with capture_logs() as logs:
        assert _complete(h, record) == ()
    assert _writes(h) == []
    assert h.config.values["DB_NAME"] == "target_db"
    assert [e["event"] for e in logs if e["event"] == "cutover_abandoned"] == [
        "cutover_abandoned"
    ]


def test_a_header_without_a_pending_target_is_a_corrupt_journal(
    tmp_path: Path,
) -> None:
    h, _ = _harness(tmp_path, mode=RestoreMode.DATABASE)
    header = dataclasses.replace(h.header(), cutover_targets={})
    record = _record(h, [STEP_CUTOVER_STARTED + "DB_NAME"], header)
    with pytest.raises(CorruptJournalError) as caught:
        _complete(h, record)
    assert caught.value.journal_path == "/j/current.json"
    assert "DB_NAME" in caught.value.reason
    assert _writes(h) == []
    assert h.database.calls == []


def test_staging_never_imports_the_database_library_itself() -> None:
    """ADR-0015 (BL-E003-098): the name check goes through adapters/database."""
    import ast
    import inspect

    from l3io.wp.backup.adapters import staging

    tree = ast.parse(inspect.getsource(staging))
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


@pytest.mark.parametrize(("name", "valid"), [("wp_site", True), ("bad`name", False)])
def test_database_name_is_valid_is_databases_check(name: str, valid: bool) -> None:
    from l3io.wp.backup.adapters.database import database_name_is_valid

    assert database_name_is_valid(name) is valid
