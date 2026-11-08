"""FR-22 / AD-16 — ``api.backup``, ``api.verify`` and ``api.restore``.

Each entry point builds the request its CLI twin builds and makes one call to
the driver that twin calls. A Run is opened per call, or the caller's
``RunContext`` is joined; the passphrase goes through the one AD-26 chain into
the Run's registry; the CLI root's exit rules are applied by calling them.

The requests are compared with the ones the CLI roots build from argv, so a
flag the CLI binds and the library drops is a failing equality, not a gap in a
hand-kept list. The AST scans' scope is every module under ``api/``, found on
disk and asserted non-empty, with a planted violation for each.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import os
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any

import pytest
from structlog.testing import capture_logs

from l3io.wp.backup import api
from l3io.wp.backup.adapters.exclusions import LoggedExclusionPolicy
from l3io.wp.backup.adapters.noop import NoopArchiveWriter
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.api import operations
from l3io.wp.backup.cli import backup as backup_cli
from l3io.wp.backup.cli import passphrase as passphrase_cli
from l3io.wp.backup.cli import restore as restore_cli
from l3io.wp.backup.cli import verify as verify_cli
from l3io.wp.backup.cli.backup import BackupRequest, storage_target_from_cli
from l3io.wp.backup.cli.context import RunContext, open_run
from l3io.wp.backup.cli.parser import build_parser
from l3io.wp.backup.cli.passphrase import PASSPHRASE_ENV
from l3io.wp.backup.cli.restore import (
    NO_SNAPSHOT_FLAG,
    DatabaseNotRestoredError,
    RelocationFlags,
    RestoreRequest,
)
from l3io.wp.backup.cli.verify import VerifyRequest
from l3io.wp.backup.core.exclusions import ExclusionSet
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.errors import (
    PassphraseUnavailableError,
    RunContextActiveError,
    UsageError,
    VerificationFailedError,
)
from l3io.wp.backup.domain.restore import ConfigPolicy, RestoreMode, SnapshotPolicy
from l3io.wp.backup.domain.secret import Secret
from tests.restore_root_rig import ALL_WAIVED, Rig, report_for
from tests.root_mode import allow_root_kwargs

pytestmark = pytest.mark.usefixtures("fake_database")

API = Path(operations.__file__).parent


class _Captured(Exception):
    """Raised by a driver spy once it has recorded its call."""


@contextmanager
def _captured() -> Iterator[None]:
    """The spy's ``_Captured``, as ``public_boundary`` delivers it: wrapped."""
    with pytest.raises(api.UnexpectedError) as caught:
        yield
    assert isinstance(caught.value.__cause__, _Captured)


def _events(logs: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [e for e in logs if e.get("event") == name]


def _run_ids(logs: list[dict[str, Any]]) -> set[str]:
    return {str(e["l3io_run_id"]) for e in logs if "l3io_run_id" in e}


@pytest.fixture
def archive(site: Path, tmp_path: Path) -> Path:
    """A real v1 Archive of ``site``, written through the API."""
    final = tmp_path / "made.tar.gz"
    with capture_logs():
        api.backup(site, scope=BackupScope.ALL, target=final)
    return final


@pytest.fixture
def passphrase() -> str:
    return f"api-pp-{uuid.uuid4().hex}"


def _spy(
    monkeypatch: pytest.MonkeyPatch,
    module: object,
    name: str,
    *,
    result: object = None,
    raise_after: bool = False,
) -> list[dict[str, Any]]:
    """Replace ``module.name`` with a recorder; it returns ``result`` or raises."""
    calls: list[dict[str, Any]] = []

    def spy(*args: Any, **kwargs: Any) -> object:  # noqa: ANN401
        calls.append({"args": args, **kwargs})
        if raise_after:
            raise _Captured
        return result

    monkeypatch.setattr(module, name, spy)
    return calls


# --------------------------------------------------------------------------
# One Run per call; a joined Run creates nothing
# --------------------------------------------------------------------------


def test_backup_and_verify_each_open_exactly_one_run(
    site: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    final = tmp_path / "out.tar.gz"
    with capture_logs() as made:
        result = api.backup(site, scope=BackupScope.ALL, target=final)
    with capture_logs() as checked:
        verified = api.verify(final)
    assert result.performed
    assert result.path == PurePosixPath(final)
    assert verified.passed
    for logs in (made, checked):
        assert len(_events(logs, "run_started")) == 1
        assert len(_events(logs, "run_finished")) == 1
        assert len(_run_ids(logs)) == 1
    assert _run_ids(made) != _run_ids(checked)
    assert capsys.readouterr().out == ""


def test_restore_opens_exactly_one_run(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    with capture_logs() as logs:
        outcome = api.restore(
            rig.site_root,
            archive=rig.archive_path,
            mode=RestoreMode.ALL,
            overrides=ALL_WAIVED,
            snapshot=SnapshotPolicy.TAKE,
            config_policy=ConfigPolicy.KEEP,
            snapshot_dir=rig.work / "snapshots",
            **allow_root_kwargs(),
        )
    assert len(_events(logs, "run_started")) == 1
    assert len(_events(logs, "restore_mode_resolved")) == 1
    assert _run_ids(logs) == {outcome.run_id}
    assert capsys.readouterr().out == ""


def _bind_real(ctx: RunContext, site: Path) -> None:
    """The outer root's one bind, as ``drive_backup`` makes it when it opens."""
    from l3io.wp.backup.adapters.database import site_descriptor_from_wp_config
    from l3io.wp.backup.cli.adapter_sets import real_adapters
    from l3io.wp.backup.cli.credentials import wordpress_source

    _, config = site_descriptor_from_wp_config(
        site.resolve(), config_file=site.resolve() / "wp-config.php"
    )
    source = wordpress_source(None, wp_config=config, environ={}, secrets=ctx.secrets)
    ctx.bind_adapters(
        real_adapters(wordpress_source=source, secrets=ctx.secrets, tracer=ctx.tracer)
    )


@pytest.mark.parametrize("entry", ["backup", "verify", "restore"])
def test_a_nested_call_without_context_is_refused(
    entry: str, site: Path, archive: Path, rig: Rig
) -> None:
    calls: dict[str, Callable[[], object]] = {
        "backup": lambda: api.backup(
            site, scope=BackupScope.ALL, target=site.parent / "n.tar.gz"
        ),
        "verify": lambda: api.verify(archive),
        "restore": lambda: api.restore(
            rig.site_root,
            archive=rig.archive_path,
            mode=RestoreMode.ALL,
            overrides=ALL_WAIVED,
            snapshot=SnapshotPolicy.TAKE,
            config_policy=ConfigPolicy.KEEP,
            **allow_root_kwargs(),
        ),
    }
    with (
        capture_logs(),
        open_run(operation="outer") as ctx,
        pytest.raises(RunContextActiveError) as caught,
    ):
        calls[entry]()
    assert caught.value.active_run_id == ctx.run_id


def test_dry_run_reaches_the_request_and_binds_the_no_op_writer(
    site: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bound: list[object] = []
    real_bind = RunContext.bind_adapters

    def bind(self: RunContext, adapters: Any) -> None:  # noqa: ANN401
        bound.append(adapters)
        real_bind(self, adapters)

    monkeypatch.setattr(RunContext, "bind_adapters", bind)
    seen: list[BackupRequest] = []
    real_drive = backup_cli.drive_backup

    def drive(request: BackupRequest, **kwargs: Any) -> Any:  # noqa: ANN401
        seen.append(request)
        return real_drive(request, **kwargs)

    monkeypatch.setattr(operations, "drive_backup", drive)
    final = tmp_path / "never.tar.gz"
    with capture_logs():
        result = api.backup(site, scope=BackupScope.ALL, target=final, dry_run=True)
    assert [r.dry_run for r in seen] == [True]
    assert len(bound) == 1
    assert isinstance(bound[0].archive_writer, NoopArchiveWriter)  # type: ignore[attr-defined]
    assert not result.performed
    assert not final.exists()


# --------------------------------------------------------------------------
# The requests: the CLI's, built from the same intent
# --------------------------------------------------------------------------


def _cli_request(
    monkeypatch: pytest.MonkeyPatch,
    module: object,
    name: str,
    root: Callable[[Any], object],
    argv: list[str],
) -> Any:  # noqa: ANN401
    """The request a ``run_*`` root builds from ``argv``, by a spy on its driver."""
    original = getattr(module, name)
    calls = _spy(monkeypatch, module, name, raise_after=True)
    with pytest.raises(_Captured):  # a CLI root: no public_boundary
        root(build_parser().parse_args(argv))
    monkeypatch.setattr(module, name, original)
    (call,) = calls
    return call["args"][0]


def test_backup_builds_the_request_run_backup_builds(
    site: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    final = tmp_path / "same.tar.gz"
    expected = _cli_request(
        monkeypatch,
        backup_cli,
        "drive_backup",
        lambda args: backup_cli.run_backup(args, secrets=SecretRegistry()),
        ["backup", "--wp-dir", str(site), "--archive", str(final), "--mode", "all"],
    )
    calls = _spy(monkeypatch, operations, "drive_backup", result="r")
    assert api.backup(site, scope=BackupScope.ALL, target=final) == "r"
    (call,) = calls
    assert call["args"] == (expected,)
    assert call["ctx"] is None
    assert call["command_args"] is None
    assert call["args"][0].exclusion == backup_cli.default_exclusion_policy()


def test_backup_requires_a_target_before_any_effect(
    site: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owner decision 2026-11-07: ``target`` is a required keyword."""
    calls = _spy(monkeypatch, operations, "drive_backup")
    parameter = inspect.signature(api.backup).parameters["target"]
    assert parameter.default is inspect.Parameter.empty
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    with pytest.raises(TypeError, match="target"):
        api.backup(site, scope=BackupScope.FILES, wp_config="conf.php")  # type: ignore[call-arg]
    assert calls == []
    api.backup(site, scope=BackupScope.FILES, target="t.tar.gz", wp_config="conf.php")
    (call,) = calls
    request: BackupRequest = call["args"][0]
    assert request.target is not None
    assert request.wp_config == Path("conf.php")
    assert request.scope is BackupScope.FILES


@pytest.mark.parametrize("name", ["backup", "restore", "verify", "doctor"])
def test_no_entry_point_takes_a_context(name: str) -> None:
    """Owner decision 2026-11-07 (BL-E006-024): no public way to join a Run."""
    assert "context" not in inspect.signature(getattr(api, name)).parameters


def test_verify_builds_the_request_run_verify_builds(
    archive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    passed = SimpleNamespace(passed=True)
    for argv, authenticate in (
        (["verify", "--archive", str(archive)], False),
        (["verify", "--archive", str(archive), "--authenticate"], True),
    ):
        expected = _cli_request(
            monkeypatch,
            verify_cli,
            "drive_verify",
            lambda args: verify_cli.run_verify(args, secrets=SecretRegistry()),
            argv,
        )
        calls = _spy(monkeypatch, operations, "drive_verify", result=passed)
        assert api.verify(archive, authenticate=authenticate) is passed
        (call,) = calls
        assert call["args"] == (expected,)
        assert call["args"][0] == VerifyRequest(
            archive=storage_target_from_cli(str(archive)), authenticate=authenticate
        )
        # A2: the environment is handed only to an authenticating verification.
        environ: Mapping[str, str] = call["environ"]
        assert (environ is os.environ) is authenticate
        if not authenticate:
            assert dict(environ) == {}
        monkeypatch.setattr(operations, "drive_verify", verify_cli.drive_verify)


def test_restore_with_every_keyword_builds_the_request_run_restore_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, passphrase: str
) -> None:
    root, archive, snaps, ca = (tmp_path / n for n in ("www", "a.tar", "s", "ca.pem"))
    admin_password = f"admin-pw-{uuid.uuid4().hex}"
    db_password = f"db-pw-{uuid.uuid4().hex}"
    argv = [
        "restore", "--wp-dir", str(root), "--archive", str(archive),
        "--mode", "all", "--cleanfirst", "--drop-database", "--force",
        "--whatIf", "--overwrite-wp-config", "--reset-existing-db-user",
        "--no-safety-snapshot", "--require-encrypted", "--snapshot-dir", str(snaps),
        "--archive-database", "wp@db.example:3307/wpdb",
        "--admin-tls", "verify-ca", "--admin-tls-ca", str(ca),
        "--resolve-interrupted-run", "run-1",
        "--admin-username", "root", "--admin-password", admin_password,
        "--new-site-url", "https://new.example", "--new-site-home-url",
        "https://home.example", "--new-db-host", "db2", "--new-db-name", "n2",
        "--new-db-user", "u2", "--new-db-password", db_password, "--allow-root",
    ]  # fmt: skip
    monkeypatch.setenv(PASSPHRASE_ENV, passphrase)
    expected: RestoreRequest = _cli_request(
        monkeypatch,
        restore_cli,
        "drive_restore",
        lambda args: restore_cli.run_restore(args, secrets=SecretRegistry()),
        argv,
    )
    monkeypatch.setenv(PASSPHRASE_ENV, passphrase)
    calls = _spy(monkeypatch, operations, "drive_restore", raise_after=True)
    with _captured():
        api.restore(
            root,
            archive=archive,
            mode=expected.mode,
            overrides=expected.overrides,
            snapshot=SnapshotPolicy.SKIP,
            config_policy=ConfigPolicy.OVERWRITE,
            snapshot_dir=snaps,
            admin=expected.admin,
            admin_tls=api.AdminTls(mode="verify-ca", ca_file=ca),
            archive_database="wp@db.example:3307/wpdb",
            reset_existing_db_user=True,
            relocation=RelocationFlags(
                new_site_url="https://new.example",
                new_home_url="https://home.example",
                new_db_host="db2",
                new_db_name="n2",
                new_db_user="u2",
                new_db_password=db_password,
                wp_config=None,
            ),
            require_encrypted=True,
            resolve_run_id="run-1",
            dry_run=True,
            allow_root=True,
        )
    (call,) = calls
    built: RestoreRequest = call["args"][0]
    assert built.cipher is not None
    assert expected.cipher is not None
    assert built.opted_out_by == "snapshot=SnapshotPolicy.SKIP"
    assert expected.opted_out_by == NO_SNAPSHOT_FLAG
    # A library call passes the Mode itself: no CLI flags to log (BL-E003-013).
    assert built.mode_flags is None
    assert expected.mode_flags is not None
    assert dataclasses.replace(built, cipher=None, opted_out_by=NO_SNAPSHOT_FLAG) == (
        dataclasses.replace(expected, cipher=None, mode_flags=None)
    )
    # The secret-bearing values are registered before the driver is called.
    secrets: SecretRegistry = call["secrets"]
    for value in (passphrase, db_password):
        assert value not in secrets.redact(f"x {value} x")


def test_restore_defaults_are_the_policy_and_no_relocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _spy(monkeypatch, operations, "drive_restore", raise_after=True)
    with _captured():
        api.restore(
            tmp_path,
            archive=tmp_path / "a.tar",
            mode=RestoreMode.FILES,
            overrides=ALL_WAIVED,
            snapshot=SnapshotPolicy.TAKE,
            config_policy=ConfigPolicy.KEEP,
            **allow_root_kwargs(),
        )
    built: RestoreRequest = calls[0]["args"][0]
    assert built.admin_tls == api.AdminTls(mode=None, ca_file=None)
    assert built.relocation == RelocationFlags.none()
    assert built.cipher is None
    assert built.extra_tools == ()
    assert built.dry_run is False
    assert built.snapshot_dir is None


def test_a_relocation_reaches_the_driver_unchanged(rig: Rig) -> None:
    """E007-S01-001 has bound ``RESTORE_EXTENSION``, so ``api/`` adds no check.

    The story's "a non-empty value is refused with ``UsageError``" held only
    while nothing was bound; the built code is followed (recorded deviation).
    """
    assert restore_cli.RESTORE_EXTENSION is not None
    flags = dataclasses.replace(
        RelocationFlags.none(), new_site_url="https://n.example"
    )
    seen: list[RestoreRequest] = []
    real = restore_cli.drive_restore

    def drive(request: RestoreRequest, **kwargs: Any) -> Any:  # noqa: ANN401
        seen.append(request)
        return real(request, **kwargs)

    rig.monkeypatch.setattr(operations, "drive_restore", drive)
    with capture_logs():
        api.restore(
            rig.site_root,
            archive=rig.archive_path,
            mode=RestoreMode.ALL,
            overrides=ALL_WAIVED,
            snapshot=SnapshotPolicy.TAKE,
            config_policy=ConfigPolicy.KEEP,
            relocation=flags,
            **allow_root_kwargs(),
        )
    assert [r.relocation for r in seen] == [flags]


# --------------------------------------------------------------------------
# A6: one Storage Target grammar, the CLI's
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["site-2026T10:00.tar.gz", "a:b", "x:y/z.tar"])
def test_a_relative_name_with_a_colon_is_a_path_not_a_url(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    target = storage_target_from_cli(name)
    assert target.scheme == "file"
    assert str(target.path) == str(tmp_path / name)


def test_a_file_url_is_still_a_url() -> None:
    assert str(storage_target_from_cli("file:///srv/a%20b.tar").path) == "/srv/a b.tar"


@pytest.mark.parametrize("text", ["file:///x?y", "file://host/x", "/srv/a\x00b"])
def test_a_target_the_cli_refuses_is_refused(text: str) -> None:
    with pytest.raises(UsageError):
        storage_target_from_cli(text)
    with pytest.raises(UsageError):
        api.verify(text)


def test_a_relative_path_becomes_absolute_as_on_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    calls = _spy(
        monkeypatch, operations, "drive_verify", result=SimpleNamespace(passed=True)
    )
    api.verify(Path("rel/a.tar.gz"))
    target = calls[0]["args"][0].archive
    assert target == storage_target_from_cli("rel/a.tar.gz")
    assert PurePosixPath(target.path).is_absolute()
    given = storage_target_from_cli(str(tmp_path / "b.tar.gz"))
    api.verify(given)
    assert calls[1]["args"][0].archive is given


# --------------------------------------------------------------------------
# A9: exclusion — the CLI's default, a caller's set logged like the CLI's
# --------------------------------------------------------------------------


def test_a_callers_exclusion_set_is_wrapped_and_a_logged_one_passed_unchanged(
    site: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _spy(monkeypatch, operations, "drive_backup")
    none = ExclusionSet.none()
    api.backup(site, scope=BackupScope.ALL, target="t.tar.gz", exclusion=none)
    wrapped = calls[0]["args"][0].exclusion
    assert isinstance(wrapped, LoggedExclusionPolicy)
    assert wrapped.policy is none
    logged = LoggedExclusionPolicy(ExclusionSet.default())
    api.backup(site, scope=BackupScope.ALL, target="t.tar.gz", exclusion=logged)
    assert calls[1]["args"][0].exclusion is logged


def test_a_callers_exclusions_are_logged(site: Path, tmp_path: Path) -> None:
    (site / "debug.log").write_text("x", encoding="utf-8")
    with capture_logs() as logs:
        api.backup(
            site,
            scope=BackupScope.FILES,
            target=tmp_path / "ex.tar.gz",
            exclusion=ExclusionSet.from_patterns(["*.log"], include_defaults=False),
        )
    (applied,) = _events(logs, "exclusions_applied")
    assert applied["source"] == "custom"
    assert [e["path"] for e in _events(logs, "path_excluded")] == ["debug.log"]


def _shape(logs: list[dict[str, Any]]) -> list[tuple[object, ...]]:
    """The records, timing, Run ID and paths aside."""
    return [
        (e.get("event"), e.get("phase"), e.get("outcome"), e.get("exit_code"))
        for e in logs
    ]


def test_an_api_backup_logs_what_the_cli_backup_logs(
    site: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with capture_logs() as cli_logs:
        backup_cli.run_backup(
            build_parser().parse_args(
                ["backup", "--wp-dir", str(site), "--archive", str(tmp_path / "c.tgz")]
            ),
            secrets=SecretRegistry(),
        )
    capsys.readouterr()
    with capture_logs() as api_logs:
        api.backup(site, scope=BackupScope.ALL, target=tmp_path / "a.tgz")
    assert _shape(api_logs) == _shape(cli_logs)
    assert any(e.get("event") == "phase_completed" for e in api_logs)


def test_an_api_verify_logs_what_the_cli_verify_logs(
    archive: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with capture_logs() as cli_logs:
        verify_cli.run_verify(
            build_parser().parse_args(["verify", "--archive", str(archive)]),
            secrets=SecretRegistry(),
        )
    capsys.readouterr()
    with capture_logs() as api_logs:
        api.verify(archive)
    assert _shape(api_logs) == _shape(cli_logs)
    assert any(e.get("event") == "phase_completed" for e in api_logs)


def test_an_unused_passphrase_variable_is_logged_as_the_cli_logs_it(
    site: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    passphrase: str,
) -> None:
    """``encrypt=False`` with the variable set: a plain Archive, said so."""
    monkeypatch.setenv(PASSPHRASE_ENV, passphrase)
    with capture_logs() as cli_logs:
        backup_cli.run_backup(
            build_parser().parse_args(
                ["backup", "--wp-dir", str(site), "--archive", str(tmp_path / "c.tgz")]
            ),
            secrets=SecretRegistry(),
        )
    capsys.readouterr()
    with capture_logs() as api_logs:
        result = api.backup(site, scope=BackupScope.ALL, target=tmp_path / "a.tgz")
    assert not result.encrypted
    assert len(_events(api_logs, "passphrase_ignored")) == 1
    assert _shape(api_logs) == _shape(cli_logs)


# --------------------------------------------------------------------------
# AD-26: one registry, one chain, one factory
# --------------------------------------------------------------------------


def test_the_passphrase_chain_is_the_clis_by_identity() -> None:
    assert operations.resolve_passphrase is passphrase_cli.resolve_passphrase
    assert operations.require_passphrase is passphrase_cli.require_passphrase
    assert operations.require_database_restored is restore_cli.require_database_restored
    assert operations.drive_backup is backup_cli.drive_backup
    assert operations.drive_verify is verify_cli.drive_verify
    assert operations.drive_restore is restore_cli.drive_restore


def test_backup_encrypt_resolves_through_require_passphrase_into_a_new_registry(
    site: Path, monkeypatch: pytest.MonkeyPatch, passphrase: str
) -> None:
    order: list[str] = []
    real_require = passphrase_cli.require_passphrase

    def require(**kwargs: Any) -> Any:  # noqa: ANN401
        order.append("require_passphrase")
        assert kwargs["environ"] is os.environ
        return real_require(**kwargs)

    tools = (object(),)
    cipher = SimpleNamespace(required_tools=lambda: tools)

    given: list[tuple[str, SecretRegistry]] = []

    def factory(*, passphrase: str, secrets: SecretRegistry) -> object:
        order.append("build_payload_cipher")
        given.append((passphrase, secrets))
        return cipher

    monkeypatch.setattr(operations, "require_passphrase", require)
    monkeypatch.setattr(operations, "build_payload_cipher", factory)
    calls = _spy(monkeypatch, operations, "drive_backup")
    api.backup(
        site,
        scope=BackupScope.ALL,
        target="t.tar.gz",
        encrypt=True,
        passphrase=passphrase,
    )
    assert order == ["require_passphrase", "build_payload_cipher"]
    (call,) = calls
    request: BackupRequest = call["args"][0]
    assert request.cipher is cipher
    assert request.extra_tools is tools
    registry: SecretRegistry = call["secrets"]
    assert given == [(Secret(passphrase), registry)]
    assert passphrase not in registry.redact(passphrase)


def test_backup_encrypt_with_no_passphrase_is_refused_before_the_site_is_read(
    site: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _spy(monkeypatch, operations, "drive_backup")
    with pytest.raises(PassphraseUnavailableError):
        api.backup(site, scope=BackupScope.ALL, target="t.tar.gz", encrypt=True)

    def closed(_label: str) -> str:
        raise EOFError

    with pytest.raises(PassphraseUnavailableError):
        api.backup(
            site, scope=BackupScope.ALL, target="t.tar.gz", encrypt=True, prompt=closed
        )
    assert calls == []


def test_a_passphrase_without_encrypt_is_refused_not_ignored(
    site: Path, monkeypatch: pytest.MonkeyPatch, passphrase: str
) -> None:
    calls = _spy(monkeypatch, operations, "drive_backup")
    with pytest.raises(UsageError, match="encrypt=True"):
        api.backup(
            site, scope=BackupScope.ALL, target="t.tar.gz", passphrase=passphrase
        )
    assert calls == []


def test_a_passphrase_without_authenticate_is_refused_not_ignored(
    archive: Path, monkeypatch: pytest.MonkeyPatch, passphrase: str
) -> None:
    calls = _spy(monkeypatch, operations, "drive_verify")
    with pytest.raises(UsageError, match="authenticate=True") as caught:
        api.verify(archive, passphrase=passphrase)
    assert caught.value.arguments == ("passphrase", "authenticate")
    assert calls == []


def test_restore_resolves_through_the_chain_with_the_restore_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, passphrase: str
) -> None:
    seen: list[dict[str, Any]] = []
    real_resolve = passphrase_cli.resolve_passphrase

    def resolve(**kwargs: Any) -> Any:  # noqa: ANN401
        seen.append(kwargs)
        return real_resolve(**kwargs)

    tools = (object(),)
    cipher = SimpleNamespace(required_tools=lambda: tools)
    built_with: list[str | None] = []

    def factory(*, passphrase: str | None, secrets: SecretRegistry) -> object:
        built_with.append(passphrase)
        return cipher

    monkeypatch.setattr(operations, "resolve_passphrase", resolve)
    monkeypatch.setattr(operations, "build_payload_cipher", factory)
    calls = _spy(monkeypatch, operations, "drive_restore", raise_after=True)
    with _captured():
        api.restore(
            tmp_path,
            archive=tmp_path / "a.tar",
            mode=RestoreMode.ALL,
            overrides=ALL_WAIVED,
            snapshot=SnapshotPolicy.TAKE,
            config_policy=ConfigPolicy.KEEP,
            passphrase=passphrase,
            **allow_root_kwargs(),
        )
    (kwargs,) = seen
    assert kwargs["label"] == restore_cli.RESTORE_PROMPT
    assert kwargs["explicit"] == passphrase
    assert kwargs["environ"] is os.environ
    assert kwargs["secrets"] is calls[0]["secrets"]
    assert built_with == [Secret(passphrase)]
    built: RestoreRequest = calls[0]["args"][0]
    assert built.cipher is cipher
    assert built.extra_tools is tools


def test_an_encrypted_backup_authenticates_with_its_passphrase(
    site: Path, tmp_path: Path, passphrase: str
) -> None:
    """The whole chain, real cipher (binder backend) and real drivers."""
    final = tmp_path / "enc.tar.gz"
    with capture_logs():
        made = api.backup(
            site,
            scope=BackupScope.ALL,
            target=final,
            encrypt=True,
            passphrase=passphrase,
        )
        checked = api.verify(final, authenticate=True, passphrase=passphrase)
    assert made.encrypted
    assert checked.passed


# --------------------------------------------------------------------------
# A3: the CLI root's exit rules, applied by calling them
# --------------------------------------------------------------------------


def test_a_failed_verification_raises_with_its_result(
    archive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failed = SimpleNamespace(passed=False, failures=())
    _spy(monkeypatch, operations, "drive_verify", result=failed)
    with pytest.raises(VerificationFailedError) as caught:
        api.verify(archive)
    assert caught.value.result is failed


def test_a_real_verification_of_a_damaged_archive_raises(
    archive: Path, tmp_path: Path
) -> None:
    damaged = tmp_path / "damaged.tar.gz"
    body = bytearray(archive.read_bytes())
    body[-600] ^= 0xFF
    damaged.write_bytes(bytes(body))
    with capture_logs() as logs, pytest.raises(VerificationFailedError):
        api.verify(damaged)
    (finished,) = _events(logs, "run_finished")
    assert finished["exit_code"] == int(VerificationFailedError.exit_code)


@pytest.fixture
def restored_rule(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    seen: list[bool] = []
    real = restore_cli.require_database_restored

    def rule(plan: Any, outcome: Any, *, dry_run: bool) -> None:  # noqa: ANN401
        seen.append(dry_run)
        real(plan, outcome, dry_run=dry_run)

    monkeypatch.setattr(operations, "require_database_restored", rule)
    return seen


def _restore_all(rig: Rig, **changes: Any) -> Any:  # noqa: ANN401
    arguments: dict[str, Any] = {
        "archive": rig.archive_path,
        "mode": RestoreMode.ALL,
        "overrides": ALL_WAIVED,
        "snapshot": SnapshotPolicy.TAKE,
        "config_policy": ConfigPolicy.KEEP,
        "snapshot_dir": rig.work / "snapshots",
    }
    with capture_logs():
        return api.restore(
            rig.site_root, **(arguments | changes), **allow_root_kwargs()
        )


def test_an_unrestored_database_raises(rig: Rig, restored_rule: list[bool]) -> None:
    rig.execute = lambda plan, *a, **kw: report_for(plan, restored=False)
    with pytest.raises(DatabaseNotRestoredError):
        _restore_all(rig)
    assert restored_rule == [False]


def test_a_dry_runs_not_performed_report_returns(
    rig: Rig, restored_rule: list[bool]
) -> None:
    rig.execute = lambda plan, *a, **kw: report_for(plan, performed=False)
    outcome = _restore_all(rig, dry_run=True)
    assert outcome.dry_run
    assert restored_rule == [True]


def test_a_skipped_snapshot_names_the_library_argument(rig: Rig) -> None:
    """The executor receives it; its ``safety_snapshot_skipped`` record names it."""
    _restore_all(rig, snapshot=SnapshotPolicy.SKIP)
    assert rig.executions[-1]["opted_out_by"] == "snapshot=SnapshotPolicy.SKIP"


# --------------------------------------------------------------------------
# AST scans of api/: one factory; extraction paired with the inner Manifest
# --------------------------------------------------------------------------


def _called(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _modules(directory: Path) -> Iterator[tuple[Path, ast.Module]]:
    for path in sorted(directory.rglob("*.py")):
        yield path, ast.parse(path.read_text(encoding="utf-8"))


def _cipher_constructions(directory: Path) -> list[str]:
    return [
        f"{path.relative_to(directory)}:{node.lineno}"
        for path, tree in _modules(directory)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _called(node) == "PassphraseCipher"
    ]


_EXTRACTORS = frozenset({"extract_archive", "decrypt_payload"})


def _unpaired_extractions(directory: Path) -> list[str]:
    """Each scope calling an extractor but not ``read_inner_manifest`` (BL-E004-060)."""
    found: list[str] = []
    for path, tree in _modules(directory):
        scopes: list[ast.AST] = [tree]
        scopes += [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
        ]
        for scope in scopes:
            body = (
                [
                    n
                    for n in tree.body
                    if not isinstance(
                        n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
                    )
                ]
                if scope is tree
                else [scope]
            )
            names = {
                _called(n)
                for top in body
                for n in ast.walk(top)
                if isinstance(n, ast.Call)
            }
            if names & _EXTRACTORS and "read_inner_manifest" not in names:
                label = getattr(scope, "name", "<module>")
                found.append(f"{path.relative_to(directory)}:{label}")
    return found


def test_the_scan_scope_is_every_api_module() -> None:
    found = {p.name for p, _ in _modules(API)}
    assert {"__init__.py", "operations.py"} <= found


def test_api_constructs_no_cipher_itself() -> None:
    assert _cipher_constructions(API) == []


def test_api_extracts_nothing_without_the_inner_manifest() -> None:
    assert _unpaired_extractions(API) == []


def test_the_api_scans_fire_on_planted_violations(tmp_path: Path) -> None:
    deep = tmp_path / "x"
    deep.mkdir()
    (deep / "leak.py").write_text(
        "def build(p):\n    return crypto.PassphraseCipher(p)\n"
        "def unpaired(t, v):\n    return tar.decrypt_payload(t, v)\n"
        "def paired(t, v):\n"
        "    plain = extract_archive(t, v)\n    read_inner_manifest(plain)\n"
        "extract_archive(1, 2)\n",
        encoding="utf-8",
    )
    assert _cipher_constructions(tmp_path) == ["x/leak.py:2"]
    assert _unpaired_extractions(tmp_path) == [
        "x/leak.py:<module>",
        "x/leak.py:unpaired",
    ]
