"""AD-4 — ``dry_run_adapters`` swaps the command halves and nothing else.

The real set's query instances and tracer are reused as they are (``is``);
every command half is replaced by its no-op, each satisfying its
``@runtime_checkable`` Protocol. The restore fields (E002-S02-012):
``restore_database_*`` are the ``database_*`` instance unless a relocated
WordPress account is bound; each placeholder-bound port raises
``UnboundAdapterError`` naming its supplier.

A driver given a Run joins it, binds nothing and inherits the outer root's
selection — except that a Dry Run request joining a real Run is refused; a
driver that opens the Run binds the set its request selects, and a Dry Run's
result claims no verification.
"""

from __future__ import annotations

import dataclasses
import inspect
import typing
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
from structlog.testing import capture_logs

from l3io.wp.backup.adapters import noop
from l3io.wp.backup.cli import adapter_sets
from l3io.wp.backup.cli import backup as backup_cli
from l3io.wp.backup.cli.adapter_sets import dry_run_adapters, real_adapters
from l3io.wp.backup.cli.context import RunContext, open_run
from l3io.wp.backup.domain.archive_format import Integrity
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.errors import UnboundAdapterError, UsageError
from l3io.wp.backup.ports.adapter_set import AdapterSet
from l3io.wp.backup.ports.archive import ArchiveStoreCommand, ArchiveWriterCommand
from l3io.wp.backup.ports.config import ConfigCommand
from l3io.wp.backup.ports.database import DatabaseCommand
from l3io.wp.backup.ports.journal import RunJournalCommand
from l3io.wp.backup.ports.search_replace import SearchReplaceCommand
from l3io.wp.backup.ports.site_files import SiteFilesCommand
from tests.port_roles import command_fields
from tests.unit.cli.conftest import FakeDatabase

pytestmark = pytest.mark.usefixtures("fake_database")

COMMANDS = {
    "archive_store_command": ArchiveStoreCommand,
    "database_command": DatabaseCommand,
    "archive_writer": ArchiveWriterCommand,
    "restore_database_command": DatabaseCommand,
    "site_files_command": SiteFilesCommand,
    "config_command": ConfigCommand,
    "search_replace": SearchReplaceCommand,
    "journal_command": RunJournalCommand,
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


# --------------------------------------------------------------------------
# The restore fields (E002-S02-012)
# --------------------------------------------------------------------------


class _Distinct:
    """A fresh database double per construction, recording what bound it."""

    def __init__(
        self, credentials: object, *, secrets: object, admin: object = None
    ) -> None:
        self.credentials, self.admin = credentials, admin

    def __repr__(self) -> str:
        return "_Distinct()"


def test_without_a_relocated_account_restore_database_is_the_same_instance(
    workdir: Path,
) -> None:
    with open_run(operation="restore") as ctx:
        real = _real(ctx)
    assert real.restore_database_query is real.database_query
    assert real.restore_database_command is real.database_command


def test_a_relocated_account_binds_a_second_database_and_keeps_the_snapshot_one(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(adapter_sets, "WpDatabase", _Distinct)
    wordpress, relocated, admin = object(), object(), object()
    with open_run(operation="restore") as ctx:
        real = real_adapters(
            wordpress_source=wordpress,  # type: ignore[arg-type]
            secrets=ctx.secrets,
            tracer=ctx.tracer,
            admin_source=admin,  # type: ignore[arg-type]
            restore_wordpress_source=relocated,  # type: ignore[arg-type]
        )
    snapshot, restore = real.database_command, real.restore_database_command
    assert isinstance(snapshot, _Distinct)
    assert isinstance(restore, _Distinct)
    assert restore is not snapshot
    assert real.restore_database_query is restore
    assert real.database_query is snapshot
    assert (snapshot.credentials, snapshot.admin) == (wordpress, admin)
    assert (restore.credentials, restore.admin) == (relocated, admin)


#: The supplier each placeholder-bound field must name — the expected outcome,
#: checked against the set the sweep below finds, not the scope itself.
SUPPLIERS = {
    "site_files_query": "E002-S02-009",
    "site_files_command": "E002-S02-009",
    "config_query": "E002-S02-009",
    "config_command": "E002-S02-009",
    "search_replace": "Epic 7",
    "journal_query": "Epic 3",
    "journal_command": "Epic 3",
}


def _members(protocol: type) -> list[str]:
    return [
        name
        for name, value in vars(protocol).items()
        if not name.startswith("_") and inspect.isfunction(value)
    ]


def _call_with_sentinels(method: Callable[..., object]) -> object:
    """Call ``method`` with an inert object for every parameter."""
    positional, keywords = [], {}
    for parameter in inspect.signature(method).parameters.values():
        if parameter.kind is parameter.KEYWORD_ONLY:
            keywords[parameter.name] = object()
        else:
            positional.append(object())
    return method(*positional, **keywords)


def sweep(
    adapter_set: object, module: ModuleType
) -> dict[str, dict[str, UnboundAdapterError | None]]:
    """Every method of every field bound to a class defined in ``module``.

    Scope, derived: a field is a placeholder when its instance's class is
    defined in ``module`` (``cli/adapter_sets.py``, where the AC puts them) —
    never by its name. Each method is every member of the field's declared
    Protocol (``typing.get_type_hints``), called with inert arguments; the
    value is the ``UnboundAdapterError`` raised, or ``None`` when it returned
    or is missing.
    """
    hints = typing.get_type_hints(type(adapter_set))
    found: dict[str, dict[str, UnboundAdapterError | None]] = {}
    for field in dataclasses.fields(adapter_set):  # type: ignore[arg-type]
        value = getattr(adapter_set, field.name)
        if type(value).__module__ != module.__name__:
            continue
        calls: dict[str, UnboundAdapterError | None] = {}
        for name in _members(hints[field.name]):
            method = getattr(value, name, None)
            if method is None:  # a missing method raises nothing either
                calls[name] = None
                continue
            try:
                _call_with_sentinels(method)
                calls[name] = None
            except UnboundAdapterError as exc:
                calls[name] = exc
        found[field.name] = calls
    return found


def test_every_placeholder_method_raises_naming_its_supplier(workdir: Path) -> None:
    with open_run(operation="restore") as ctx:
        real = _real(ctx)
    found = sweep(real, adapter_sets)
    assert set(found) == set(SUPPLIERS)
    for field, calls in found.items():
        assert calls, f"{field}: its Protocol has no method"
        for method, error in calls.items():
            assert error is not None, f"{field}.{method} returned instead of raising"
            assert SUPPLIERS[field] in error.supplier
            assert error.port.endswith(f".{method}")
            assert error.port in str(error)
            assert error.supplier in str(error)


def test_the_sweep_sees_a_placeholder_by_module_not_name(workdir: Path) -> None:
    """Scope attack: a misnamed placeholder, and one method that returns."""
    planted = ModuleType("planted_root")
    exec(  # noqa: S102 - a fixed planted module
        "from l3io.wp.backup.domain.errors import UnboundAdapterError\n"
        "class PendingConfig:\n"
        "    def read_constants(self, config_file, keys):\n"
        "        raise UnboundAdapterError('x.read_constants', supplier='s')\n"
        "    def set_constants(self, config_file, values):\n"
        "        return None\n",
        planted.__dict__,
    )
    with open_run(operation="restore") as ctx:
        real = _real(ctx)
    pending = planted.PendingConfig()
    seeded = dataclasses.replace(real, config_query=pending, config_command=pending)
    found = sweep(seeded, planted)
    assert set(found) == {"config_query", "config_command"}
    assert found["config_command"]["set_constants"] is None
    assert found["config_command"]["save_copy"] is None  # missing
    assert found["config_query"]["read_constants"] is not None


def test_supplied_search_replace_and_journal_are_bound_as_given(
    workdir: Path,
) -> None:
    replacer, journal_query, journal_command = object(), object(), object()
    with open_run(operation="restore") as ctx:
        real = real_adapters(
            wordpress_source=object(),  # type: ignore[arg-type]
            secrets=ctx.secrets,
            tracer=ctx.tracer,
            search_replace=replacer,  # type: ignore[arg-type]
            journal=(journal_query, journal_command),  # type: ignore[arg-type]
        )
    assert real.search_replace is replacer
    assert real.journal_query is journal_query
    assert real.journal_command is journal_command


def test_every_wp_database_restore_method_is_a_stub_behind_the_admin_check() -> None:
    from l3io.wp.backup.adapters.database import WpDatabase
    from l3io.wp.backup.adapters.redaction import SecretRegistry
    from l3io.wp.backup.domain.errors import AdminCredentialsRequiredError
    from l3io.wp.backup.ports.database import DatabaseQuery

    bare = WpDatabase(_Source(), secrets=SecretRegistry())  # type: ignore[arg-type]
    admin = WpDatabase(
        _Source(),  # type: ignore[arg-type]
        secrets=SecretRegistry(),
        admin=_Source(),  # type: ignore[arg-type]
    )
    assert not bare.administrator_bound
    assert admin.administrator_bound
    # Every member of both Protocols but the two backup methods implemented.
    restore = sorted(
        {*_members(DatabaseQuery), *_members(DatabaseCommand)}
        - {"check_dump", "dump_to"}
    )
    assert len(restore) == 9
    for name in restore:
        with pytest.raises(UnboundAdapterError) as caught:
            _call_with_sentinels(getattr(admin, name))
        assert caught.value.supplier == "E002-S02-009"
        assert caught.value.port == f"WpDatabase.{name}"
        if name == "load_dump":
            # Loads as the WordPress account: no administrator check.
            with pytest.raises(UnboundAdapterError):
                _call_with_sentinels(bare.load_dump)
        else:
            with pytest.raises(AdminCredentialsRequiredError, match="--admin-username"):
                _call_with_sentinels(getattr(bare, name))


def test_dry_run_reads_the_admin_binding_wp_database_exposes(workdir: Path) -> None:
    """The root reads ``administrator_bound`` off the real adapter itself."""
    from l3io.wp.backup.adapters.database import WpDatabase
    from l3io.wp.backup.adapters.redaction import SecretRegistry
    from l3io.wp.backup.domain.database import DatabaseLocation

    admin = WpDatabase(
        _Source(),  # type: ignore[arg-type]
        secrets=SecretRegistry(),
        admin=_Source(),  # type: ignore[arg-type]
    )
    with open_run(operation="restore") as ctx:
        real = dataclasses.replace(_real(ctx), restore_database_command=admin)
    dry = dry_run_adapters(real).restore_database_command
    assert dry.drop_database(DatabaseLocation("db", 3306, "wp"), confirm="wp") is False


class _Source:
    """A credential source double; never resolved."""

    source = "test"


def test_dry_run_noop_database_carries_the_admin_binding(workdir: Path) -> None:
    from l3io.wp.backup.domain.database import DatabaseLocation
    from l3io.wp.backup.domain.errors import AdminCredentialsRequiredError

    where = DatabaseLocation("db", 3306, "wp")

    class Bound:
        administrator_bound = True

    with open_run(operation="restore") as ctx:
        real = _real(ctx)
    without = dry_run_adapters(real)
    with pytest.raises(AdminCredentialsRequiredError):
        without.restore_database_command.drop_database(where, confirm="wp")
    bound = dataclasses.replace(real, restore_database_command=Bound())  # type: ignore[arg-type]
    with_admin = dry_run_adapters(bound)
    assert (
        with_admin.restore_database_command.drop_database(where, confirm="wp") is False
    )
    with pytest.raises(AdminCredentialsRequiredError):
        with_admin.database_command.drop_database(where, confirm="wp")
