"""AD-7 items 1-4 — no credential on any surface this package emits (ADR-0018).

Driven by ``credential_sentinel`` (``tests/conftest.py``): a unique password
planted in a temporary ``wp-config.php``, then every structlog event and
stdlib record at every level, the CLI's stdout and stderr, ``str``/``repr``/
traceback of every exception the drivers raise (unredacted — the primary
control), the ``repr`` of what the composition root builds, every
``subprocess.Popen`` argv and ``env``, ``os.environ`` and the Archive's
Manifest bytes are searched for it.

The unit tier runs over a database double that records what it was handed and
resolves the credentials as database's ``dump`` does; the ``integration`` tier
runs against the CI database.

**Epic 3 story 3.1's obligation:** apply ``credential_sentinel`` to a full
restore (success and an authentication failure) in this module.
``test_a_restore_driver_has_a_hygiene_case`` fails once ``core/restore`` exists
without a test here whose name contains ``restore``.
"""

from __future__ import annotations

import ast
import inspect
import os
import stat
import sys
import tarfile
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from tests.conftest import CredentialSentinel

wpdb = pytest.importorskip("l3io.wp.database")

from l3io.wp.backup.adapters.redaction import REDACTED  # noqa: E402
from l3io.wp.backup.cli import backup as backup_cli  # noqa: E402
from l3io.wp.backup.cli import main  # noqa: E402
from l3io.wp.backup.cli import verify as verify_cli  # noqa: E402
from l3io.wp.backup.cli.parser import build_parser  # noqa: E402
from l3io.wp.backup.cli.passphrase import resolve_passphrase  # noqa: E402
from l3io.wp.backup.domain.archive_format import MANIFEST_MEMBER  # noqa: E402
from l3io.wp.backup.domain.errors import (  # noqa: E402
    BackupError,
    CredentialsError,
    DumpFailedError,
)
from tests.conftest import write_wp_config  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CORE = PROJECT_ROOT / "src" / "l3io" / "wp" / "backup" / "core"


class DatabaseDouble:
    """Stands in for database's ``dump``; records what it was handed."""

    def __init__(self, *, fail: bool = False, transfer: str | None = None) -> None:
        self.fail = fail
        self.transfer = transfer
        self.handed: list[object] = []

    def __call__(
        self, connection: object, credentials: object, to: Path, **kw: object
    ) -> object:
        self.handed.extend((connection, credentials, kw))
        resolved = credentials.resolve()  # type: ignore[attr-defined]
        if self.fail:
            msg = f"the account {resolved.username!r} was rejected"
            raise wpdb.AuthenticationFailedError(msg)
        if self.transfer is not None:
            # The tool's stderr, as database hands it over: untrusted text.
            msg = "dump failed"
            raise wpdb.TransferError(msg, wpdb.Verdict("dump", ()), 2, self.transfer)
        to.write_bytes(b"-- dump\n")
        to.chmod(stat.S_IRUSR | stat.S_IWUSR)
        verdict = wpdb.Verdict("dump", ())
        return wpdb.TransferResult(
            bytes=8, duration_ms=1.0, tool="double", server=None, verdict=verdict
        )


def _called_names(module: object, *drivers: str) -> set[str]:
    """Every bare name the drivers call — derived from their source."""
    tree = ast.parse(inspect.getsource(module))  # type: ignore[arg-type]
    functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name in drivers
    ]
    assert len(functions) == len(drivers), "a driver was renamed"
    return {
        node.func.id
        for function in functions
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def watched_names(module: object, *drivers: str) -> set[str]:
    """The package callables the drivers call: what the root builds."""
    return {
        name
        for name in _called_names(module, *drivers)
        if getattr(getattr(module, name, None), "__module__", "").startswith("l3io.wp.")
    }


def _watch_root(sentinel: CredentialSentinel) -> None:
    names = watched_names(backup_cli, "run_backup", "backup_from_args")
    for name in names:
        sentinel.watch(backup_cli, name)


def _manifest(archive: Path) -> bytes:
    with tarfile.open(archive) as tar:
        member = tar.extractfile(MANIFEST_MEMBER)
        assert member is not None
        return member.read()


def _backup(
    sentinel: CredentialSentinel,
    capsys: pytest.CaptureFixture[str],
    *,
    mode: str,
) -> tuple[int, Path]:
    archive = sentinel.workdir / f"backup-{mode}.tar.gz"
    argv = ["backup", "--wp-dir", str(sentinel.site), "--archive", str(archive)]
    code = main([*argv, "--mode", mode])
    out = capsys.readouterr()
    sentinel.add("stdout", out.out)
    sentinel.add("stderr", out.err)
    return code, archive


@pytest.fixture
def double(monkeypatch: pytest.MonkeyPatch) -> DatabaseDouble:
    fake = DatabaseDouble()
    monkeypatch.setattr(wpdb, "dump", fake)
    return fake


# --------------------------------------------------------------------------
# Unit tier — over the database double
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["all", "db"])
def test_a_backup_emits_no_credential(
    credential_sentinel: CredentialSentinel,
    double: DatabaseDouble,
    capsys: pytest.CaptureFixture[str],
    mode: str,
) -> None:
    _watch_root(credential_sentinel)
    code, archive = _backup(credential_sentinel, capsys, mode=mode)
    assert code == 0
    credential_sentinel.add("manifest", _manifest(archive))
    for i, handed in enumerate(double.handed):
        credential_sentinel.add_repr(f"database double handed {i}", handed)
    assert double.handed, "the double was never called: the Run dumped nothing"
    credential_sentinel.assert_clean()


def test_the_resolved_password_is_registered_before_it_could_be_emitted(
    credential_sentinel: CredentialSentinel,
    double: DatabaseDouble,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[backup_cli.SecretRegistry] = []
    real = backup_cli.SecretRegistry

    def spy() -> backup_cli.SecretRegistry:
        created.append(real())
        return created[-1]

    monkeypatch.setattr(backup_cli, "SecretRegistry", spy)
    code, _ = _backup(credential_sentinel, capsys, mode="db")
    assert code == 0
    assert len(created) == 1
    redacted = created[0].redact(credential_sentinel.password) == REDACTED
    assert redacted, "the resolved password was not registered"
    events = [e for e in credential_sentinel.logs if e["event"] == "secrets_registered"]
    assert [e["count"] for e in events] == [1]


def test_a_backup_failing_at_authentication_emits_no_credential(
    credential_sentinel: CredentialSentinel,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(wpdb, "dump", DatabaseDouble(fail=True))
    _watch_root(credential_sentinel)
    code, archive = _backup(credential_sentinel, capsys, mode="all")
    assert code != 0
    assert not archive.exists()
    args = build_parser().parse_args(
        ["backup", "--wp-dir", str(credential_sentinel.site), "--archive", str(archive)]
    )
    with pytest.raises(CredentialsError) as caught:
        backup_cli.backup_from_args(args, secrets=backup_cli.SecretRegistry())
    credential_sentinel.add_exception("backup driver", caught.value)
    credential_sentinel.assert_clean()


def test_a_source_failing_with_the_secret_in_its_text_is_not_chained(
    credential_sentinel: CredentialSentinel,
    double: DatabaseDouble,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unknown failure's text is untrusted: named by class, never chained."""
    password = credential_sentinel.password

    def leaky(self: object) -> object:
        msg = f"cannot use {password}"
        raise ValueError(msg)

    monkeypatch.setattr(wpdb.WpConfigFile, "resolve", leaky)
    args = build_parser().parse_args(
        ["backup", "--wp-dir", str(credential_sentinel.site), "--mode", "db"]
    )
    with pytest.raises(CredentialsError) as caught:
        backup_cli.backup_from_args(args, secrets=backup_cli.SecretRegistry())
    assert caught.value.reason == "ValueError"
    credential_sentinel.add_exception("backup driver", caught.value)
    assert main(["backup", "--wp-dir", str(credential_sentinel.site)]) != 0
    out = capsys.readouterr()
    credential_sentinel.add("stderr", out.err)
    credential_sentinel.assert_clean()


def test_verify_emits_no_credential(
    credential_sentinel: CredentialSentinel,
    double: DatabaseDouble,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, archive = _backup(credential_sentinel, capsys, mode="all")
    assert code == 0
    names = watched_names(verify_cli, "run_verify", "verify_from_args")
    assert {"SecretRegistry", "storage_target_from_cli", "verify_archive"} <= names
    for name in names:
        credential_sentinel.watch(verify_cli, name)
    assert main(["verify", "--archive", str(archive)]) == 0
    missing = credential_sentinel.workdir / "absent.tar.gz"
    assert main(["verify", "--archive", str(missing)]) != 0
    out = capsys.readouterr()
    credential_sentinel.add("verify stdout", out.out)
    credential_sentinel.add("verify stderr", out.err)
    args = build_parser().parse_args(["verify", "--archive", str(missing)])
    with pytest.raises(BackupError) as caught:
        verify_cli.verify_from_args(args)
    credential_sentinel.add_exception("verify driver", caught.value)
    credential_sentinel.assert_clean()


def test_the_watched_set_is_derived_and_covers_the_credential_path() -> None:
    names = watched_names(backup_cli, "run_backup", "backup_from_args")
    assert {
        "SecretRegistry",
        "site_descriptor_from_wp_config",
        "wordpress_source",
        "WpDatabase",
        "execute_backup_plan",
    } <= names
    assert "Path" not in names  # not a package callable


def test_a_failed_transfer_emits_no_credential(
    credential_sentinel: CredentialSentinel,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stderr = f"mysqldump: error near {credential_sentinel.password}"
    monkeypatch.setattr(wpdb, "dump", DatabaseDouble(transfer=stderr))
    code, _ = _backup(credential_sentinel, capsys, mode="db")
    assert code != 0
    args = build_parser().parse_args(
        ["backup", "--wp-dir", str(credential_sentinel.site), "--mode", "db"]
    )
    with pytest.raises(DumpFailedError) as caught:
        backup_cli.backup_from_args(args, secrets=backup_cli.SecretRegistry())
    credential_sentinel.add_exception("backup driver", caught.value)
    credential_sentinel.assert_clean()


@pytest.mark.parametrize("credential_sentinel", [{"passphrase": True}], indirect=True)
def test_a_passphrase_in_the_environment_is_never_emitted(
    credential_sentinel: CredentialSentinel,
    double: DatabaseDouble,
    capsys: pytest.CaptureFixture[str],
) -> None:
    passphrase = credential_sentinel.passphrase
    assert passphrase is not None
    registry = backup_cli.SecretRegistry()
    resolved = resolve_passphrase(
        explicit=None, environ=os.environ, prompt=None, secrets=registry
    )
    assert resolved is not None
    same = resolved.value == passphrase
    assert same, "the environment step did not supply the planted passphrase"
    redacted = registry.redact(passphrase) == REDACTED
    assert redacted, "the passphrase was not registered"
    credential_sentinel.add_repr("resolved passphrase", resolved)
    credential_sentinel.add_repr("registry", registry)
    # Encryption is not requested: the variable is noted, its value is not.
    code, archive = _backup(credential_sentinel, capsys, mode="all")
    assert code == 0
    credential_sentinel.add("manifest", _manifest(archive))
    ignored = [
        e for e in credential_sentinel.logs if e["event"] == "passphrase_ignored"
    ]
    assert len(ignored) == 1
    credential_sentinel.assert_clean()


# --------------------------------------------------------------------------
# The fixture itself: a planted leak on each kind of surface is caught
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "surface", ["structlog", "stdlib below DEBUG", "popen env", "repr", "exception"]
)
def test_the_fixture_catches_a_leak_on_every_surface(
    credential_sentinel: CredentialSentinel, surface: str
) -> None:
    import logging
    import subprocess

    import structlog

    secret = credential_sentinel.password
    if surface == "structlog":
        structlog.get_logger("planted").debug("planted", nested={"k": [secret]})
    elif surface == "stdlib below DEBUG":
        logging.getLogger("planted").log(1, "planted %s", secret)
    elif surface == "popen env":
        subprocess.Popen([sys.executable, "-c", "pass"], env={"X": secret}).wait()
    elif surface == "repr":
        credential_sentinel.add_repr("planted", {"password": secret})
    else:
        try:
            raise ValueError(secret)  # noqa: TRY301
        except ValueError as exc:
            planted = RuntimeError("wrapper")
            planted.__cause__ = exc
            credential_sentinel.add_exception("planted", planted)
    with pytest.raises(AssertionError) as caught:
        credential_sentinel.assert_clean()
    assert secret not in str(caught.value)


def test_the_fixture_catches_a_changed_environment(
    credential_sentinel: CredentialSentinel, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MYSQL_PWD", "planted")
    with pytest.raises(AssertionError, match=r"os\.environ"):
        credential_sentinel.assert_clean()


# --------------------------------------------------------------------------
# Epic 3's obligation
# --------------------------------------------------------------------------


def test_a_restore_driver_has_a_hygiene_case() -> None:
    """Tripwire: ``core/restore`` may not land without a restore case here."""
    exists = (CORE / "restore.py").exists() or (CORE / "restore").is_dir()
    cases = [
        name
        for name in globals()
        if name.startswith("test_")
        and "restore" in name
        and name != "test_a_restore_driver_has_a_hygiene_case"
    ]
    assert not exists or cases, (
        "core/restore exists: Epic 3 story 3.1 must apply credential_sentinel "
        "to a full restore in this module"
    )


# --------------------------------------------------------------------------
# Integration tier — against the CI database
# --------------------------------------------------------------------------

live = pytest.mark.skipif(
    "DB_HOST" not in os.environ, reason="needs the CI database (DB_HOST unset)"
)


@pytest.fixture
def live_account(credential_sentinel: CredentialSentinel) -> Iterator[str]:
    """A throwaway account whose password is the sentinel.

    CI's own account is not used: its password is in the job environment and
    is not unique, so it could not be told apart from a leak. The CI root
    connection only creates and drops the account.
    """
    pymysql = pytest.importorskip("pymysql")
    user = f"wpb_{uuid.uuid4().hex[:12]}"
    root = pymysql.connect(
        host=os.environ["DB_HOST"],
        user=os.environ.get("DB_USER", "root"),
        password=os.environ["DB_PASSWORD"],
        autocommit=True,
    )
    with root.cursor() as cursor:
        cursor.execute(
            "CREATE USER %s@'%%' IDENTIFIED BY %s", (user, credential_sentinel.password)
        )
        cursor.execute("GRANT ALL PRIVILEGES ON *.* TO %s@'%%'", (user,))
    write_wp_config(
        credential_sentinel.config, password=credential_sentinel.password, user=user
    )
    try:
        yield user
    finally:
        with root.cursor() as cursor:
            cursor.execute("DROP USER IF EXISTS %s@'%%'", (user,))
        root.close()


@pytest.mark.integration
@live
@pytest.mark.usefixtures("live_account")
@pytest.mark.parametrize("mode", ["all", "db"])
def test_a_live_backup_emits_no_credential(
    credential_sentinel: CredentialSentinel,
    capsys: pytest.CaptureFixture[str],
    mode: str,
) -> None:
    _watch_root(credential_sentinel)
    code, archive = _backup(credential_sentinel, capsys, mode=mode)
    assert code == 0
    credential_sentinel.add("manifest", _manifest(archive))
    assert credential_sentinel.popen, "the live dump spawned no client tool"
    credential_sentinel.assert_clean()


@pytest.mark.integration
@live
def test_a_live_backup_failing_at_authentication_emits_no_credential(
    credential_sentinel: CredentialSentinel,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The sentinel is no account's password here, so the server rejects it."""
    _watch_root(credential_sentinel)
    code, archive = _backup(credential_sentinel, capsys, mode="db")
    assert code != 0
    assert not archive.exists()
    args = build_parser().parse_args(
        ["backup", "--wp-dir", str(credential_sentinel.site), "--mode", "db"]
    )
    with pytest.raises(CredentialsError) as caught:
        backup_cli.backup_from_args(args, secrets=backup_cli.SecretRegistry())
    credential_sentinel.add_exception("backup driver", caught.value)
    credential_sentinel.assert_clean()
