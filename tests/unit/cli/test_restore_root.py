"""E003-S02-006 — the restore root: order, binding, the seams it fills, its output.

Driven over :class:`tests.restore_root_rig.Rig`: the real ``drive_restore``,
the real adapter set with recorded database halves, and a recorded
``execute_restore``. Scopes are derived from their source of truth —
``FLAG_MAP`` keys, ``RestoreMode``, ``dataclasses.fields(RestoreOutcome)`` —
never a hand list.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import json
import os
from pathlib import Path, PurePosixPath

import pytest
from structlog.testing import capture_logs

pytest.importorskip("l3io.wp.database")

from l3io.wp.backup.adapters.journal import CURRENT, FileRunJournal, journal_directory
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.cli import main
from l3io.wp.backup.cli import restore as restore_mod
from l3io.wp.backup.cli.context import open_run
from l3io.wp.backup.cli.passphrase import PASSPHRASE_ENV
from l3io.wp.backup.cli.restore import (
    ArchiveIdentityUnconfirmedError,
    DatabaseNotRestoredError,
    NonEmptyTargetRefusal,
    RestoreOutcome,
)
from l3io.wp.backup.core.restore import (
    IncompleteRunError,
    SnapshotFailedError,
    SnapshotResult,
    staging_name,
)
from l3io.wp.backup.domain.archive_format import Integrity
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.database import DatabaseTargetState
from l3io.wp.backup.domain.errors import (
    ArchiveNotEncryptedError,
    LoadFailedError,
    PreflightFailedError,
    UnreadableSiteRootError,
    UsageError,
)
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.domain.flag_map import FLAG_MAP, FlagRejection, resolve_flags
from l3io.wp.backup.domain.journal import JournalHeader
from l3io.wp.backup.domain.preflight import ToolRequirement
from l3io.wp.backup.domain.restore import (
    ConfigPolicy,
    OverrideSet,
    RestoreMode,
    RestorePhase,
    SnapshotPolicy,
    restores_database,
    restores_files,
)
from tests.conftest import CredentialSentinel
from tests.restore_harness import ARCHIVE_DB
from tests.restore_root_rig import ALL_WAIVED, Rig, report_for
from tests.unit.cli.conftest import RecordingSiteLock

#: Every table key that expresses a Mode (scope from the table).
MODE_KEYS = [k for k, e in FLAG_MAP.items() if not isinstance(e, FlagRejection)]
DATABASE_MODES = [m for m in RestoreMode if restores_database(m)]
FILE_ONLY_MODES = [m for m in RestoreMode if not restores_database(m)]
EMPTY_DB = DatabaseTargetState(False, 0, False, None, None, 0, 0)


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    return Rig(tmp_path, monkeypatch)


def _no_config(rig: Rig) -> None:
    rig.config.unlink()


# --------------------------------------------------------------------------
# The Mode, resolved once, reaches every phase unchanged
# --------------------------------------------------------------------------


@pytest.mark.parametrize("key", MODE_KEYS, ids=[str(k) for k in MODE_KEYS])
def test_the_mode_reaches_the_plan_the_header_and_the_snapshot_origin(
    rig: Rig, key: object
) -> None:
    mode, _ = resolve_flags(key)  # type: ignore[arg-type]
    # Every waiver: this case is about the Mode, not FR-7's refusal.
    rig.drive(mode=mode, overrides=ALL_WAIVED)
    assert rig.plans[-1].mode is mode
    assert rig.inputs.header.restore_mode is mode
    rig.take_snapshot()
    origin = rig.backups[-1]["origin"]
    assert origin.restore_mode == mode.value
    assert origin.run_id == rig.inputs.run_id


# --------------------------------------------------------------------------
# Step order, the one bind, the joined Run
# --------------------------------------------------------------------------


def test_the_steps_run_in_order_and_nothing_is_commanded_before_the_executor(
    rig: Rig,
) -> None:
    lock = RecordingSiteLock(rig.events)
    restore_mod.drive_restore(
        rig.request(), ctx=None, secrets=SecretRegistry(), site_lock=lock
    )
    names = [e if isinstance(e, str) else e[0] for e in rig.events]
    order = [
        "lock.acquire",
        "journal.incomplete",
        "real_adapters",
        "survey",
        "build_restore_plan",
        "execute_restore",
    ]
    assert [names.index(step) for step in order] == sorted(
        names.index(step) for step in order
    )
    assert "db_command" not in names[: names.index("execute_restore")]
    assert names.count("real_adapters") == 1
    (call,) = rig.adapter_calls
    assert call["journal"] is not None
    assert "admin_source" in call
    # The root runs neither PREFLIGHT nor VERIFY itself (ADR-0022).
    assert "check_dump" not in names


def test_a_joined_run_binds_nothing_and_uses_the_outer_set(rig: Rig) -> None:
    with open_run(
        operation="outer",
        site_root=rig.site_root.resolve(),
        site_lock=RecordingSiteLock(),
    ) as ctx:
        outer = restore_mod.real_adapters(
            wordpress_source=object(),  # type: ignore[arg-type]
            secrets=ctx.secrets,
            tracer=ctx.tracer,
            journal=(
                FileRunJournal(journal_directory(rig.site_root)),
                FileRunJournal(journal_directory(rig.site_root)),
            ),
        )
        ctx.bind_adapters(outer)
        calls = len(rig.adapter_calls)
        outcome = restore_mod.drive_restore(rig.request(), ctx=ctx)
        assert outcome.run_id == ctx.run_id
    assert len(rig.adapter_calls) == calls
    assert rig.executions[-1]["ports"].adapters is outer


def test_join_or_open_receives_the_requests_dry_run(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[bool] = []
    real = restore_mod.join_or_open

    def recording(*args: object, **kwargs: object) -> object:
        seen.append(bool(kwargs["dry_run"]))
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(restore_mod, "join_or_open", recording)
    rig.drive(dry_run=True)
    rig.drive(dry_run=False)
    assert seen == [True, False]


def test_no_function_below_run_restore_receives_args() -> None:
    for name, function in inspect.getmembers(restore_mod, inspect.isfunction):
        if function.__module__ != restore_mod.__name__ or name == "run_restore":
            continue
        assert "args" not in inspect.signature(function).parameters, name


# --------------------------------------------------------------------------
# The executor's slots
# --------------------------------------------------------------------------


def test_restore_ports_is_built_once_over_ctx_adapters(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []
    real = restore_mod.restore_ports

    def recording(adapters: object, **kwargs: object) -> object:
        calls.append(adapters)
        return real(adapters, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(restore_mod, "restore_ports", recording)
    rig.drive()
    call = rig.executions[-1]
    ports = call["ports"]
    assert calls == [ports.adapters]
    assert call["check_database"].keywords["ports"] is ports
    assert call["load_database"].keywords["ports"] is ports
    assert call["load_database"].keywords["workspace"] == Path(call["inputs"].workspace)
    unmet = ToolRequirement("no-such-tool-e003", None, "the test")
    with pytest.raises(PreflightFailedError):
        ports.preflight((unmet,))


def test_opted_out_by_is_never_none_under_skip(rig: Rig) -> None:
    """BL-E003-050."""
    rig.drive(snapshot=SnapshotPolicy.SKIP)
    assert rig.executions[-1]["opted_out_by"] == restore_mod.NO_SNAPSHOT_FLAG
    with pytest.raises(ValueError, match="opted_out_by"):
        rig.request(opted_out_by="")


def test_the_header_snapshot_path_is_absolute_and_normalised(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BL-E003-051: the runner compares the path as a string."""
    monkeypatch.chdir(rig.work)
    rig.drive(snapshot_dir=Path("x/../snapshots"))
    planned = rig.inputs.header.snapshot_path
    assert planned is not None
    assert Path(planned).is_absolute()
    assert planned == os.path.normpath(planned)
    assert Path(planned).parent == (rig.work / "snapshots").resolve()
    rig.take_snapshot()
    target = rig.backups[-1]["args"][2]
    assert str(target.path) == planned


def test_the_header_names_no_snapshot_path_when_none_is_planned(rig: Rig) -> None:
    """ADV-S02-01: the undo text must not point at a file never written."""
    rig.drive(snapshot=SnapshotPolicy.SKIP)
    assert rig.inputs.header.snapshot_path is None


def test_the_archive_copy_is_refused_before_it_fills_the_workspace(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADV-S02-02: BL-E002-116 holds for the Archive copy too."""
    from collections import namedtuple

    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(
        restore_mod.shutil, "disk_usage", lambda _path: usage(10, 10, 1)
    )
    with pytest.raises(restore_mod.InsufficientSpaceError, match="workspace"):
        rig.drive(mode=RestoreMode.FILES, snapshot=SnapshotPolicy.SKIP)


@pytest.mark.parametrize("dry_run", [False, True], ids=["real", "dry-run"])
def test_the_root_creates_no_snapshot_directory_itself(rig: Rig, dry_run: bool) -> None:
    """M-4 (AD-4): the store that stages the snapshot creates its directory.

    The root has no ``dry_run`` branch for it: in either Run it only binds
    ``create_archive_directory``, and a Dry Run's store twin creates nothing.
    """
    snapshot_dir = rig.work / "snapshots"
    rig.drive(dry_run=dry_run)
    assert RestorePhase.SNAPSHOT in rig.plans[-1].phases
    assert rig.adapter_calls[-1]["create_archive_directory"] is True
    assert not snapshot_dir.exists()


@pytest.mark.parametrize("has_config", [True, False], ids=["config", "no-config"])
def test_backup_receives_the_config_the_run_uses(rig: Rig, has_config: bool) -> None:
    changes: dict[str, object] = {}
    if not has_config:
        _no_config(rig)
        changes["archive_database"] = (
            f"{ARCHIVE_DB['DB_USER']}@{ARCHIVE_DB['DB_HOST']}/{ARCHIVE_DB['DB_NAME']}"
        )
        changes["reset_existing_db_user"] = True  # the rig's account exists
    modes: list[int] = []

    def snapshot_then_report(*args: object, **kwargs: object) -> object:
        rig.take_snapshot()
        modes.append(rig.backups[-1]["config"].path.stat().st_mode & 0o777)
        return report_for(rig.executions[-1]["plan"])

    rig.execute = snapshot_then_report
    rig.drive(**changes)
    config = rig.backups[-1]["config"]
    if has_config:
        assert config.path == rig.config.resolve()
        assert config.location is not None
    else:
        assert config.location is None
        assert config.path.is_relative_to(Path(rig.inputs.workspace))
        assert modes == [0o600]


# --------------------------------------------------------------------------
# BL-E003-035: the database is surveyed in every Mode that restores it
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", list(RestoreMode))
def test_the_live_database_is_surveyed_in_every_database_mode(
    rig: Rig, mode: RestoreMode
) -> None:
    rig.drive(mode=mode)
    survey = rig.executions[-1]["survey_target"]()
    surveyed = [e for e in rig.events if isinstance(e, tuple) and e[0] == "survey"]
    if restores_database(mode):
        assert survey.database == rig.survey
        assert surveyed
        assert surveyed[0][2] == "target_user"  # the site's DB_USER (AD-29)
    else:
        assert survey.database is None
        assert not surveyed
    assert rig.inputs.live_database_exists is restores_database(mode)


def test_live_database_exists_follows_the_survey(rig: Rig) -> None:
    rig.survey = EMPTY_DB
    rig.drive(mode=RestoreMode.DATABASE)
    assert rig.inputs.live_database_exists is False


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", FILE_ONLY_MODES + DATABASE_MODES)
def test_overwrite_without_files_is_usage_before_the_lock(
    rig: Rig, mode: RestoreMode
) -> None:
    lock = RecordingSiteLock()
    request = rig.request(mode=mode, config_policy=ConfigPolicy.OVERWRITE)
    if restores_files(mode):
        restore_mod.drive_restore(request, ctx=None, site_lock=lock)
        return
    with pytest.raises(UsageError, match="--overwrite-wp-config"):
        restore_mod.drive_restore(request, ctx=None, site_lock=lock)
    assert lock.events == []


def test_the_fr7_refusal_is_in_cli_spelling_and_logged_once(rig: Rig) -> None:
    with capture_logs() as logs, pytest.raises(NonEmptyTargetRefusal) as caught:
        rig.drive(mode=RestoreMode.FILES, overrides=OverrideSet.none())
    text = str(caught.value)
    assert "--mode files --force would authorize it" in text
    assert "(Restore Mode files)" in text
    assert "mode=" not in text
    assert caught.value.exit_code is ExitCode.REFUSED
    (refused,) = [e for e in logs if e["event"] == "restore_refused"]
    assert refused["authorizing"] == "--mode files --force"
    assert refused["reason"] == "non_empty_target"
    assert refused["parts"] == ["files"]
    assert not rig.executions


def test_target_checked_names_the_waivers(rig: Rig) -> None:
    with capture_logs() as logs:
        rig.drive()
    (checked,) = [e for e in logs if e["event"] == "target_checked"]
    assert checked["overrides"] == sorted(
        o.value for o in rig.plans[-1].overrides.waived
    )
    assert checked["table_count"] == rig.survey.table_count


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores a mode-000 directory")
def test_an_unreadable_site_root_is_refused(rig: Rig) -> None:
    """BL-E003-034, through the root: REFUSED, never read as empty."""
    stated = f"{ARCHIVE_DB['DB_USER']}@{ARCHIVE_DB['DB_HOST']}/{ARCHIVE_DB['DB_NAME']}"
    rig.site_root.chmod(0o000)
    try:
        with pytest.raises(UnreadableSiteRootError) as caught:
            rig.drive(mode=RestoreMode.FILES, archive_database=stated)
    finally:
        rig.site_root.chmod(0o755)
    assert caught.value.exit_code is ExitCode.REFUSED
    assert not rig.executions


# --------------------------------------------------------------------------
# The journal: refusal and resolution
# --------------------------------------------------------------------------


def _interrupted(rig: Rig) -> str:
    journal = FileRunJournal(journal_directory(rig.site_root))
    header = JournalHeader(
        run_id="interrupted-run-1",
        started_at="2026-11-03T00:00:00+00:00",
        site_root=PurePosixPath(rig.site_root.resolve()),
        restore_mode=RestoreMode.FILES,
        archive=str(rig.archive_path),
        snapshot_path=None,
        live_database_before="target_db",
        staging_database=None,
        wp_config_path=str(rig.config),
        load_database=staging_name("target_db", "interrupted-run-1"),
        cutover_keys=(),
        cutover_targets={},
        prior_constants={},
        phase_undo={},
        saved_config=None,
    )
    journal.begin_run(header)
    return header.run_id


def test_an_incomplete_journal_refuses_before_the_bind(rig: Rig) -> None:
    _interrupted(rig)
    with pytest.raises(IncompleteRunError) as caught:
        rig.drive()
    assert "interrupted-run-1" in str(caught.value)
    assert not rig.adapter_calls


def test_a_different_resolve_id_is_refused_naming_the_journals(rig: Rig) -> None:
    _interrupted(rig)
    with pytest.raises(IncompleteRunError, match="interrupted-run-1"):
        rig.drive(resolve_run_id="some-other-run")


@pytest.mark.parametrize("dry_run", [True, False], ids=["dry", "real"])
def test_resolution_stops_the_run(rig: Rig, dry_run: bool) -> None:
    run_id = _interrupted(rig)
    current = journal_directory(rig.site_root) / CURRENT
    outcome = rig.drive(resolve_run_id=run_id, dry_run=dry_run)
    assert outcome.resolved_run_id == run_id
    assert outcome.phases == ()
    assert not rig.executions
    assert current.exists() is dry_run


# --------------------------------------------------------------------------
# BL-E002-114: the Archive's database identity
# --------------------------------------------------------------------------

_ARCHIVE_ID = f"{ARCHIVE_DB['DB_USER']}@{ARCHIVE_DB['DB_HOST']}/{ARCHIVE_DB['DB_NAME']}"


@pytest.mark.parametrize(
    ("stated", "differs"),
    [
        (None, None),
        (f"{ARCHIVE_DB['DB_USER']}@db.target/{ARCHIVE_DB['DB_NAME']}", ("host",)),
        (
            f"{ARCHIVE_DB['DB_USER']}@{ARCHIVE_DB['DB_HOST']}/another_site_db",
            ("database",),
        ),
        (f"existing_admin@{ARCHIVE_DB['DB_HOST']}/{ARCHIVE_DB['DB_NAME']}", ("user",)),
        (
            f"{ARCHIVE_DB['DB_USER']}@{ARCHIVE_DB['DB_HOST']}:3307/{ARCHIVE_DB['DB_NAME']}",
            ("port",),
        ),
    ],
    ids=["absent", "operator-host", "other-db", "other-user", "other-port"],
)
def test_an_unconfirmed_archive_identity_is_refused_before_any_connection(
    rig: Rig, stated: str | None, differs: tuple[str, ...] | None
) -> None:
    _no_config(rig)
    with pytest.raises(ArchiveIdentityUnconfirmedError) as caught:
        rig.drive(archive_database=stated)
    assert caught.value.exit_code is ExitCode.REFUSED
    text = str(caught.value)
    # L-4: only which part differs is named; the operator's text never is.
    assert caught.value.differs == differs
    assert ARCHIVE_DB["DB_PASSWORD"] not in text
    # RT-S02-003 (owner, 2026-11-06, A): never a ready-to-paste confirmation.
    assert "--archive-database " + ARCHIVE_DB["DB_USER"] not in text
    if stated is None:
        # Absent: the Archive's claim is not shown, so it cannot be pasted back.
        for value in ("DB_HOST", "DB_NAME", "DB_USER"):
            assert ARCHIVE_DB[value] not in text
        assert "USER@HOST[:PORT]/NAME" in text
    else:
        assert stated not in text
        for value in ("DB_HOST", "DB_NAME", "DB_USER"):
            assert ARCHIVE_DB[value] in text
    assert rig.database is not None
    assert [e for e in rig.events if isinstance(e, tuple)] == []
    assert not rig.executions


def test_a_matching_archive_identity_proceeds(rig: Rig) -> None:
    _no_config(rig)
    spelled = _ARCHIVE_ID.replace(ARCHIVE_DB["DB_HOST"], ARCHIVE_DB["DB_HOST"].upper())
    rig.drive(archive_database=spelled, reset_existing_db_user=True)
    assert rig.executions
    assert rig.inputs.site.db_name == ARCHIVE_DB["DB_NAME"]


# --- RT-S02-004 (owner decision, 2026-11-06, A) ----------------------------------


def test_an_existing_account_named_by_the_archive_is_refused_before_any_effect(
    rig: Rig, credential_sentinel: CredentialSentinel
) -> None:
    """No target config, the account exists, no flag: REFUSED, nothing run."""
    _no_config(rig)
    assert rig.survey.user_exists is True
    with pytest.raises(restore_mod.ExistingDatabaseUserError) as caught:
        rig.drive(archive_database=_ARCHIVE_ID)
    assert caught.value.exit_code is ExitCode.REFUSED
    text = str(caught.value)
    assert ARCHIVE_DB["DB_USER"] in text
    assert "--reset-existing-db-user" in text
    assert ARCHIVE_DB["DB_PASSWORD"] not in text
    assert not rig.executions
    calls = [e[0] for e in rig.events if isinstance(e, tuple)]
    assert set(calls) <= {"survey"}, f"only the administrator's survey ran: {calls}"
    credential_sentinel.add_exception("existing account", caught.value)
    credential_sentinel.assert_clean()


def test_a_dry_run_refuses_an_existing_account_the_same_way(rig: Rig) -> None:
    _no_config(rig)
    with pytest.raises(restore_mod.ExistingDatabaseUserError):
        rig.drive(archive_database=_ARCHIVE_ID, dry_run=True)


@pytest.mark.parametrize(
    "case", ["flag-given", "account-absent", "target-has-config", "files-only"]
)
def test_an_existing_account_is_accepted_when_the_rule_does_not_apply(
    rig: Rig, case: str
) -> None:
    changes: dict[str, object] = {"archive_database": _ARCHIVE_ID}
    if case != "target-has-config":
        _no_config(rig)
    else:
        changes = {}
    if case == "flag-given":
        changes["reset_existing_db_user"] = True
    if case == "account-absent":
        rig.survey = dataclasses.replace(rig.survey, user_exists=False)
    if case == "files-only":
        changes["mode"] = RestoreMode.FILES
    rig.drive(**changes)
    assert rig.executions


def test_a_malformed_archive_database_is_usage_before_the_lock(rig: Rig) -> None:
    lock = RecordingSiteLock()
    request = rig.request(archive_database="no-at-sign/db")
    with pytest.raises(UsageError, match="USER@HOST"):
        restore_mod.drive_restore(request, ctx=None, site_lock=lock)
    assert lock.events == []


# --------------------------------------------------------------------------
# BL-E003-036 (RT-001): every read after the bind is of the verified copy
# --------------------------------------------------------------------------


def test_the_executor_reads_the_workspace_copy_not_the_storage_target(
    rig: Rig,
) -> None:
    def swap_then_run(*args: object, **kwargs: object) -> object:
        # The Storage Target's file is replaced after the root bound it.
        rig.archive_path.write_bytes(b"swapped by an attacker")
        return restore_mod_real_execute(*args, **kwargs)

    restore_mod_real_execute = __import__(
        "l3io.wp.backup.adapters.restore_runner", fromlist=["execute_restore"]
    ).execute_restore
    rig.execute = swap_then_run
    rig.drive(mode=RestoreMode.FILES, snapshot=SnapshotPolicy.SKIP)
    copy = Path(rig.inputs.archive.path)
    assert copy.is_relative_to(Path(rig.inputs.workspace))
    assert (rig.site_root / "index.php").read_bytes() == b"<?php // restored index\n"


# --------------------------------------------------------------------------
# What the driver returns and what the root prints
# --------------------------------------------------------------------------


def test_drive_restore_prints_nothing(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    with capture_logs():
        rig.drive()
        rig.drive(dry_run=True)
    assert capsys.readouterr().out == ""


#: Fields a run without Epic 7 or a resolution leaves ``None`` or empty, by design.
_EMPTY_BY_DESIGN = {"search_replace", "resolved_run_id", "skipped_links"}


@pytest.mark.parametrize("dry_run", [False, True], ids=["real", "dry"])
def test_every_outcome_field_is_populated(rig: Rig, dry_run: bool) -> None:
    def snapshotting(*args: object, **kwargs: object) -> object:
        snapshot = SnapshotResult(
            path=PurePosixPath(rig.inputs.header.snapshot_path or ""),
            scope=BackupScope.ALL,
            encrypted=False,
            bytes=1,
            integrity=Integrity.VERIFIED,
            performed=not dry_run,
        )
        report = report_for(rig.executions[-1]["plan"], performed=not dry_run)
        return dataclasses.replace(report, snapshot=snapshot, wp_config_kept=True)

    rig.execute = snapshotting
    outcome = rig.drive(dry_run=dry_run)
    for f in dataclasses.fields(RestoreOutcome):
        value = getattr(outcome, f.name)
        if f.name in _EMPTY_BY_DESIGN:
            assert not value, f.name
        elif not isinstance(value, bool):
            assert value is not None, f.name
            assert value != (), f.name
    assert outcome.dry_run is dry_run
    assert outcome.actions == rig.plans[-1].actions


def _argv(rig: Rig, *extra: str) -> list[str]:
    return [
        "--log-format",
        "json",
        "restore",
        "--wp-dir",
        str(rig.site_root),
        "--archive",
        str(rig.archive_path),
        "--snapshot-dir",
        str(rig.work / "snapshots"),
        "--force",
        *extra,
    ]


def _summary(out: str) -> dict[str, object]:
    """UX-S02-04: ``--log-format json`` stdout is exactly one ``restore`` object."""
    (line,) = out.splitlines()
    decoded = json.loads(line)
    assert set(decoded) == {"restore"}
    summary = decoded["restore"]
    assert isinstance(summary, dict)
    return summary


def test_run_restore_prints_the_report_and_applies_the_exit_rule(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(_argv(rig)) == int(ExitCode.OK)
    summary = _summary(capsys.readouterr().out)
    assert summary["status"] == "completed"
    report = summary["database_report"]
    assert isinstance(report, dict)
    assert report["restored"] is True
    rig.execute = lambda *a, **k: report_for(a[0], restored=False)  # type: ignore[misc]
    assert main(_argv(rig)) == int(DatabaseNotRestoredError.exit_code)
    captured = capsys.readouterr()
    assert "DatabaseNotRestoredError" in captured.err
    assert _summary(captured.out)["status"] == "failed"


def test_a_dry_run_prints_its_plan_and_exits_ok(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    rig.execute = lambda *a, **k: report_for(a[0], performed=False)  # type: ignore[misc]
    assert main(["--whatIf", *_argv(rig)]) == int(ExitCode.OK)
    out = capsys.readouterr().out
    for action in rig.plans[-1].actions:
        assert action.text in out


def test_a_failure_after_the_snapshot_names_it(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    """AG-15: the error carries the snapshot path and the root prints it."""

    def fail_after_snapshot(*args: object, **kwargs: object) -> object:
        rig.take_snapshot(BackupScope.ALL)
        reason = "exit 1"
        raise LoadFailedError(reason, database="wp")

    rig.execute = fail_after_snapshot
    real_result = restore_mod.snapshot_result

    def performed(path: PurePosixPath, **kwargs: object) -> SnapshotResult:
        result = real_result(path, **kwargs)  # type: ignore[arg-type]
        return dataclasses.replace(result, performed=True, integrity=Integrity.VERIFIED)

    rig.monkeypatch.setattr(restore_mod, "snapshot_result", performed)
    with pytest.raises(LoadFailedError) as caught:
        rig.drive()
    planned = rig.inputs.header.snapshot_path
    assert str(caught.value.snapshot_path) == planned
    capsys.readouterr()
    assert main(_argv(rig)) == int(LoadFailedError.exit_code)
    captured = capsys.readouterr()
    assert _summary(captured.out) == {
        "status": "failed",
        "error": "LoadFailedError",
        "snapshot_path": rig.inputs.header.snapshot_path,
    }
    assert "safety snapshot is NOT encrypted" in captured.err


def _performed_snapshots(rig: Rig) -> None:
    real_result = restore_mod.snapshot_result

    def performed(path: PurePosixPath, **kwargs: object) -> SnapshotResult:
        result = real_result(path, **kwargs)  # type: ignore[arg-type]
        return dataclasses.replace(result, performed=True, integrity=Integrity.VERIFIED)

    rig.monkeypatch.setattr(restore_mod, "snapshot_result", performed)


# --------------------------------------------------------------------------
# BL-E003-075 (ADV-S02-07): the one passphrase, asked before the probe
# --------------------------------------------------------------------------


def test_the_restore_prompt_says_both_uses_and_that_empty_means_none(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Asked before the Archive is probed, so it cannot say "Archive" alone."""
    monkeypatch.delenv(PASSPHRASE_ENV, raising=False)
    shown: list[str] = []

    def prompt(label: str) -> str:
        shown.append(label)
        return ""

    monkeypatch.setattr(restore_mod, "terminal_prompt", lambda: prompt)
    assert main(_argv(rig)) == int(ExitCode.OK)
    assert shown == [restore_mod.RESTORE_PROMPT]
    assert "Safety Snapshot" in shown[0]
    assert "empty for none" in shown[0]
    assert rig.executions[-1]["cipher"] is None
    capsys.readouterr()


@pytest.mark.parametrize("credential_sentinel", [{"passphrase": True}], indirect=True)
def test_an_encrypted_snapshot_says_so_without_the_passphrase(
    rig: Rig,
    credential_sentinel: CredentialSentinel,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A passphrase given for an unencrypted Archive still encrypts the snapshot."""

    def fail_after_snapshot(*args: object, **kwargs: object) -> object:
        rig.take_snapshot(BackupScope.ALL)
        reason = "exit 1"
        raise LoadFailedError(reason, database="wp")

    rig.execute = fail_after_snapshot
    _performed_snapshots(rig)
    assert main(_argv(rig)) == int(LoadFailedError.exit_code)
    err = capsys.readouterr().err
    planned = rig.inputs.header.snapshot_path
    assert "safety snapshot is encrypted with the passphrase" in err
    assert str(planned) in err
    assert "NOT encrypted" not in err
    assert credential_sentinel.passphrase is not None
    assert credential_sentinel.passphrase not in err


@pytest.mark.parametrize(
    "error",
    [RuntimeError("a defect"), OSError(5, "I/O error"), KeyboardInterrupt()],
    ids=["defect", "os-error", "interrupt"],
)
def test_any_error_after_the_snapshot_names_it(
    rig: Rig, capsys: pytest.CaptureFixture[str], error: BaseException
) -> None:
    """ADV-S02-06: not only a ``BackupError`` carries the Safety Snapshot path."""

    def fail_after_snapshot(*args: object, **kwargs: object) -> object:
        rig.take_snapshot(BackupScope.ALL)
        raise error

    rig.execute = fail_after_snapshot
    _performed_snapshots(rig)
    with pytest.raises(type(error)) as caught:
        rig.drive()
    planned = rig.inputs.header.snapshot_path
    assert str(caught.value.snapshot_path) == planned  # type: ignore[union-attr]
    if not isinstance(error, Exception):
        return  # main() does not catch KeyboardInterrupt; drive_restore named it
    capsys.readouterr()
    assert main(_argv(rig)) == int(ExitCode.FAILED)
    summary = _summary(capsys.readouterr().out)
    assert summary == {
        "status": "failed",
        "error": type(error).__name__,
        "snapshot_path": rig.inputs.header.snapshot_path,
    }


@pytest.mark.parametrize("spelling", ["--whatIf", "--what-if"])
def test_what_if_after_restore_is_a_dry_run(
    rig: Rig, capsys: pytest.CaptureFixture[str], spelling: str
) -> None:
    """UX-S02-02: the flag works on either side of ``restore``."""
    rig.execute = lambda *a, **k: report_for(a[0], performed=False)  # type: ignore[misc]
    assert main([*_argv(rig), spelling]) == int(ExitCode.OK)
    summary = _summary(capsys.readouterr().out)
    assert summary["status"] == "planned"
    assert summary["dry_run"] is True
    assert summary["actions"] == [a.text for a in rig.plans[-1].actions]


def test_a_run_without_what_if_is_not_a_dry_run(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    """The subcommand's flag defaults to absent, so it never overrides the root's."""
    assert main(_argv(rig)) == int(ExitCode.OK)
    assert _summary(capsys.readouterr().out)["dry_run"] is False
    assert main(["--whatIf", *_argv(rig)]) == int(ExitCode.OK)
    assert _summary(capsys.readouterr().out)["dry_run"] is True


def _console_argv(rig: Rig, *extra: str) -> list[str]:
    argv = _argv(rig, *extra)
    return ["--log-format", "console", *argv[2:]]


@pytest.mark.parametrize("dry_run", [False, True], ids=["real", "dry-run"])
def test_a_files_only_restore_ends_with_its_completion_line(
    rig: Rig, capsys: pytest.CaptureFixture[str], dry_run: bool
) -> None:
    """UX-S02-04: a Run with nothing else to report still says it finished."""
    root = ["--whatIf"] if dry_run else []
    argv = [*root, *_console_argv(rig, "--mode", "files", "--no-safety-snapshot")]
    assert main(argv) == int(ExitCode.OK)
    out = capsys.readouterr().out.splitlines()
    what = f"Restore Mode files, archive {rig.archive_path}, site {rig.site_root}"
    expected = (
        f"Dry Run complete ({what}); nothing was changed"
        if dry_run
        else f"restore complete ({what})"
    )
    assert out[-1] == expected


def test_a_failed_database_check_prints_no_completion_line(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    rig.execute = lambda *a, **k: report_for(a[0], restored=False)  # type: ignore[misc]
    assert main(_console_argv(rig)) == int(DatabaseNotRestoredError.exit_code)
    out = capsys.readouterr().out
    assert "database: NOT restored" in out
    assert "restore complete" not in out


# --------------------------------------------------------------------------
# AD-4: real and Dry Run plans are equal; RestorePhase is driven, not listed
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", list(RestoreMode))
def test_the_real_and_the_dry_run_plan_are_equal(rig: Rig, mode: RestoreMode) -> None:
    rig.drive(mode=mode)
    rig.drive(mode=mode, dry_run=True)
    real, dry = rig.plans[-2:]
    assert real == dry


def _named_phases(source: str) -> list[int]:
    """Lines naming a ``RestorePhase`` member other than as ``X in <y>.phases``."""
    tree = ast.parse(source)
    allowed: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Compare)
            and len(node.ops) == 1
            and isinstance(node.ops[0], ast.In | ast.NotIn)
            and isinstance(node.comparators[0], ast.Attribute)
            and node.comparators[0].attr == "phases"
        ):
            allowed.add(id(node.left))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "RestorePhase"
        and id(node) not in allowed
    ]


def test_the_root_names_no_phase_except_through_plan_phases() -> None:
    source = Path(inspect.getfile(restore_mod)).read_text(encoding="utf-8")
    assert _named_phases(source) == []


def test_a_listed_phase_is_caught() -> None:
    planted = "for p in (RestorePhase.VERIFY, RestorePhase.CLEAN): pass\n"
    assert _named_phases(planted) == [1, 1]
    assert _named_phases("x = RestorePhase.SNAPSHOT in plan.phases\n") == []


# --- RT-002 waiver (ADR-0016 amendment, 2026-11-06) ------------------------------


def _structural_only(monkeypatch: pytest.MonkeyPatch) -> None:
    real_view = restore_mod._Archive.view  # noqa: SLF001

    def view(self: object) -> object:
        return dataclasses.replace(
            real_view(self),  # type: ignore[arg-type]
            integrity=Integrity.STRUCTURAL_ONLY,
        )

    monkeypatch.setattr(restore_mod._Archive, "view", view)  # noqa: SLF001


def test_an_unencrypted_archive_is_restored_with_the_warning_logged_first(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    """Restore, never refuse; the WARNING precedes every effect."""
    seen: list[list[str]] = []
    with capture_logs() as logs:

        def execute(*args: object, **kwargs: object) -> object:
            seen.append([str(e["event"]) for e in logs])
            return report_for(args[0])  # type: ignore[arg-type]

        rig.execute = execute
        rig.drive()
    (before,) = seen
    assert "archive_unauthenticated" in before
    (record,) = [e for e in logs if e["event"] == "archive_unauthenticated"]
    assert record["log_level"] == "warning"
    assert record["integrity"] == Integrity.VERIFIED.value
    err = capsys.readouterr().err
    assert err.count(restore_mod.UNAUTHENTICATED_WARNING) == 1  # UX-S02-01
    assert "STRUCTURAL_ONLY" not in err


def test_require_encrypted_refuses_a_plain_archive_before_any_effect(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    """RT-1: ``--require-encrypted`` turns the plain-archive warning into a refusal.

    The refusal is ``ArchiveNotEncryptedError`` (a ``UsageError``, exit 2): the
    one ``verify --authenticate`` raises for the same archive.
    """
    executed: list[object] = []
    rig.execute = lambda *a, **k: executed.append(a)  # type: ignore[misc, assignment]
    with pytest.raises(ArchiveNotEncryptedError):
        rig.drive(require_encrypted=True)
    assert executed == []
    assert ArchiveNotEncryptedError.exit_code is ExitCode.USAGE
    assert main(_argv(rig, "--require-encrypted")) == int(ExitCode.USAGE)
    assert executed == []
    assert restore_mod.UNAUTHENTICATED_WARNING not in capsys.readouterr().err


def test_a_structural_only_archive_is_named_on_stderr_even_when_the_run_fails(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review L-1: the stderr warning is written before the executor runs."""
    _structural_only(monkeypatch)

    def fail(*args: object, **kwargs: object) -> object:
        reason = "exit 1"
        raise LoadFailedError(reason, database="wp")

    rig.execute = fail
    with capture_logs():
        assert main(_argv(rig)) != int(ExitCode.OK)
    err = capsys.readouterr().err
    assert err.count(restore_mod.STRUCTURAL_ONLY_WARNING) == 1
    assert restore_mod.UNAUTHENTICATED_WARNING not in err


@pytest.mark.parametrize("dry_run", [False, True], ids=["real", "dry-run"])
def test_only_a_real_run_refuses_a_snapshot_that_was_not_performed(
    rig: Rig, dry_run: bool
) -> None:
    """ADV-5/AD-1: the executor accepts a not-performed snapshot from its result
    alone; the root, which alone knows the Run is real, refuses it."""
    rig.snapshot_performed = False
    rig.drive(dry_run=dry_run)
    if dry_run:
        assert rig.take_snapshot().performed is False
    else:
        with pytest.raises(SnapshotFailedError, match="not performed"):
            rig.take_snapshot()
