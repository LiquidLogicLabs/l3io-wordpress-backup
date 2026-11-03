"""AD-4 — ``dry_run_adapters`` swaps the command halves and nothing else.

The real set's query instances and tracer are reused as they are (``is``);
every command half is replaced by its no-op, each satisfying its
``@runtime_checkable`` Protocol. A driver given a Run joins it, binds nothing
and inherits the outer root's selection — except that a Dry Run request joining
a real Run is refused; a driver that opens the Run binds the set its request
selects, and a Dry Run's result claims no verification.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from l3io.wp.backup.adapters import noop
from l3io.wp.backup.cli import backup as backup_cli
from l3io.wp.backup.cli.adapter_sets import dry_run_adapters, real_adapters
from l3io.wp.backup.cli.context import RunContext, open_run
from l3io.wp.backup.domain.archive_format import Integrity
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.errors import UsageError
from l3io.wp.backup.ports.adapter_set import AdapterSet
from l3io.wp.backup.ports.archive import ArchiveStoreCommand, ArchiveWriterCommand
from l3io.wp.backup.ports.database import DatabaseCommand
from tests.port_roles import command_fields
from tests.unit.cli.conftest import FakeDatabase

pytestmark = pytest.mark.usefixtures("fake_database")

COMMANDS = {
    "archive_store_command": ArchiveStoreCommand,
    "database_command": DatabaseCommand,
    "archive_writer": ArchiveWriterCommand,
}


def _real(ctx: RunContext) -> AdapterSet:
    return real_adapters(
        wordpress_source=object(),  # type: ignore[arg-type]
        secrets=ctx.secrets,
        tracer=ctx.tracer,
    )


def _request(site: Path, final: Path, *, dry_run: bool) -> backup_cli.BackupRequest:
    return backup_cli.BackupRequest(
        site_root=site,
        scope=BackupScope.ALL,
        target=backup_cli.storage_target_from_cli(str(final)),
        wp_config=None,
        credentials=None,
        cipher=None,
        dry_run=dry_run,
    )


def test_queries_and_tracer_are_shared_and_commands_replaced(workdir: Path) -> None:
    with open_run(operation="backup") as ctx:
        real = _real(ctx)
        dry = dry_run_adapters(real)
    for field in dataclasses.fields(AdapterSet):
        value, original = getattr(dry, field.name), getattr(real, field.name)
        if field.name in COMMANDS:
            assert value is not original
            assert type(value).__module__ == noop.__name__
            assert isinstance(value, COMMANDS[field.name])
        else:
            assert value is original, field.name
    assert dry.tracer is real.tracer


def test_the_command_fields_named_here_are_every_command_field() -> None:
    assert set(command_fields(AdapterSet)) == set(COMMANDS)


def test_the_noops_share_one_action_log(workdir: Path) -> None:
    log = noop.ActionLog()
    with open_run(operation="backup") as ctx:
        dry = dry_run_adapters(_real(ctx), log=log)
    dry.archive_store_command.stage(
        backup_cli.storage_target_from_cli(str(workdir / "x.tar"))
    ).discard()
    assert [c.method for c in log.calls] == ["stage"]
    assert not (workdir / "x.tar").exists()


def test_a_driver_that_opens_a_dry_run_binds_the_noop_set(
    site: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bound: list[AdapterSet] = []
    real_bind = RunContext.bind_adapters

    def bind(self: RunContext, adapter_set: AdapterSet) -> None:
        bound.append(adapter_set)
        real_bind(self, adapter_set)

    monkeypatch.setattr(RunContext, "bind_adapters", bind)
    final = tmp_path / "rehearsed.tar.gz"
    with capture_logs() as logs:
        result = backup_cli.drive_backup(_request(site, final, dry_run=True), ctx=None)
    (adapter_set,) = bound
    assert isinstance(adapter_set.database_command, noop.NoopDatabase)
    assert isinstance(adapter_set.archive_writer, noop.NoopArchiveWriter)
    assert isinstance(adapter_set.archive_store_command, noop.NoopArchiveStore)
    assert result.performed is False
    assert result.integrity is None  # nothing was verified
    assert not final.exists()
    started = [e for e in logs if e["event"] == "run_started"]
    assert [e["dry_run"] for e in started] == [True]


def test_a_joined_driver_binds_nothing_and_inherits_the_outer_selection(
    site: Path,
    tmp_path: Path,
    workdir: Path,
    fake_database: FakeDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    final = tmp_path / "joined.tar.gz"
    with open_run(operation="restore", dry_run=True) as ctx:
        ctx.bind_adapters(dry_run_adapters(_real(ctx)))
        binds: list[object] = []
        monkeypatch.setattr(
            RunContext, "bind_adapters", lambda self, s: binds.append(s)
        )
        # The request asks for a real Run; the outer root chose a Dry Run.
        result = backup_cli.drive_backup(_request(site, final, dry_run=False), ctx=ctx)
    assert binds == []
    assert result.performed is False
    assert not final.exists()
    assert fake_database.calls == []
    assert len(fake_database.checks) == 1  # the query still ran


def test_a_driver_that_opens_a_real_run_reports_verified(
    site: Path, tmp_path: Path, workdir: Path
) -> None:
    final = tmp_path / "real.tar.gz"
    with capture_logs():
        result = backup_cli.drive_backup(_request(site, final, dry_run=False), ctx=None)
    assert result.performed is True
    assert result.integrity is Integrity.VERIFIED
    assert final.is_file()


def test_the_root_selection_is_on_the_context(workdir: Path) -> None:
    with open_run(operation="backup", dry_run=True) as dry:
        assert dry.dry_run is True
    with open_run(operation="backup") as real:
        assert real.dry_run is False


def test_a_dry_run_request_cannot_join_a_real_run(
    site: Path, tmp_path: Path, workdir: Path, fake_database: FakeDatabase
) -> None:
    final = tmp_path / "joined-real.tar.gz"
    with open_run(operation="restore") as ctx:
        ctx.bind_adapters(_real(ctx))
        with pytest.raises(UsageError, match="cannot join"):
            backup_cli.drive_backup(_request(site, final, dry_run=True), ctx=ctx)
    assert not final.exists()
    assert fake_database.calls == []
    assert fake_database.checks == []  # refused before anything was asked
