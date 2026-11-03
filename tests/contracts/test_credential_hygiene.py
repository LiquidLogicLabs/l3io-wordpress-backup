"""AD-7 items 1-4 — no credential on any surface this package emits (ADR-0018).

Driven by ``credential_sentinel`` (``tests/conftest.py``): a unique password
planted in a temporary ``wp-config.php``, then every structlog event and
stdlib record at every level, the CLI's stdout and stderr, ``str``/``repr``/
traceback of every exception the drivers raise (unredacted — the primary
control), the ``repr`` of what the composition root builds, every
``subprocess.Popen`` argv and ``env``, ``os.environ`` and the Archive's
Manifest bytes are searched for it.

The CLI cases run ``main`` at ``--log-level DEBUG``, so the events searched are
those ``cli/logging.py``'s configured pipeline emitted — redaction installed
through the Run Context — and the rendered stderr it wrote (FR-20: raising
verbosity never introduces a credential). ``_backup`` asserts the pipeline was
live, so the check cannot pass over an empty stream.

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

import l3io.wp.backup.cli as cli_root  # noqa: E402
from l3io.wp.backup.adapters.redaction import REDACTED  # noqa: E402
from l3io.wp.backup.cli import adapter_sets, main  # noqa: E402
from l3io.wp.backup.cli import backup as backup_cli  # noqa: E402
from l3io.wp.backup.cli import verify as verify_cli  # noqa: E402
from l3io.wp.backup.cli.passphrase import resolve_passphrase  # noqa: E402
from l3io.wp.backup.domain.archive_format import MANIFEST_MEMBER  # noqa: E402
from l3io.wp.backup.domain.backup_scope import BackupScope  # noqa: E402
from l3io.wp.backup.domain.errors import (  # noqa: E402
    BackupError,
    CredentialsError,
    DumpFailedError,
)
from tests.conftest import (  # noqa: E402
    backdoor_plain_payload,
    forge_archive,
    seal_archive,
    write_wp_config,
)

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


def _ready(connection: object, credentials: object, **kw: object) -> object:
    """Stands in for database's ``check_dump``: nothing blocks the dump."""
    return wpdb.Verdict("dump", ())


@pytest.fixture(autouse=True)
def _dump_readiness(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preflight's ``check_dump`` (AD-13) passes over the double; a live test
    keeps the real one."""
    if request.node.get_closest_marker("integration") is None:
        monkeypatch.setattr(wpdb, "check_dump", _ready)


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


BACKUP_DRIVERS = ("run_backup", "drive_backup", "backup")
#: ``verify --authenticate`` (BL-E002-060): ``_authenticate`` resolves and uses
#: the passphrase.
VERIFY_DRIVERS = ("run_verify", "drive_verify", "_authenticate")
#: Parameters that receive AD-26's inputs — the passphrase or the environment
#: holding it — by design. A callable taking one is a passphrase *source*: its
#: arguments hold the secret, so it is not spied on; what it returns and emits
#: still is. Derived from each signature, not listed.
PASSPHRASE_INPUTS = frozenset({"environ", "explicit", "passphrase"})


def takes_a_passphrase(owner: object, name: str) -> bool:
    """Whether ``owner.name``'s signature has a :data:`PASSPHRASE_INPUTS` parameter."""
    try:
        parameters = inspect.signature(getattr(owner, name)).parameters
    except (TypeError, ValueError):
        return False
    return bool(PASSPHRASE_INPUTS & parameters.keys())


#: The CLI root creates the Run's SecretRegistry and reports its failure.
ROOT_DRIVERS = ("main",)


def _watch_root(sentinel: CredentialSentinel) -> None:
    for name in watched_names(cli_root, *ROOT_DRIVERS):
        sentinel.watch(cli_root, name)
    for name in watched_names(backup_cli, *BACKUP_DRIVERS):
        sentinel.watch(backup_cli, name)
    for name in watched_names(adapter_sets, "real_adapters"):
        sentinel.watch(adapter_sets, name)


def _drive(sentinel: CredentialSentinel, scope: BackupScope = BackupScope.ALL) -> None:
    """The library path: the driver, given a request rather than flags."""
    target = str(sentinel.workdir / f"driven-{scope.value}.tar.gz")
    request = backup_cli.BackupRequest(
        site_root=sentinel.site,
        scope=scope,
        target=backup_cli.storage_target_from_cli(target),
        wp_config=None,
        credentials=None,
        cipher=None,
    )
    backup_cli.drive_backup(request, ctx=None, secrets=backup_cli.SecretRegistry())


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
    rehearse: bool = False,
) -> tuple[int, Path]:
    archive = sentinel.workdir / f"backup-{mode}.tar.gz"
    argv = ["backup", "--wp-dir", str(sentinel.site), "--archive", str(archive)]
    root = ["--log-level", "DEBUG", "--log-format", "json"]
    if rehearse:
        root.append("--whatIf")
    code = main([*root, *argv, "--mode", mode])
    out = capsys.readouterr()
    sentinel.add("stdout", out.out)
    sentinel.add("stderr", out.err)
    assert '"event": "run_started"' in out.err, "the configured pipeline wrote nothing"
    assert any(e["event"] == "run_finished" for e in sentinel.logs)
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


def test_a_dry_run_emits_no_credential(
    credential_sentinel: CredentialSentinel,
    double: DatabaseDouble,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """FR-19: a Dry Run resolves real credentials; its ``planned_action``
    rendering and its stdout plan carry none of them."""
    _watch_root(credential_sentinel)
    code, archive = _backup(credential_sentinel, capsys, mode="all", rehearse=True)
    assert code == 0
    assert not archive.exists()
    assert double.handed == [], "a Dry Run reached the dump"
    planned = [e for e in credential_sentinel.logs if e["event"] == "planned_action"]
    assert planned, "the Dry Run logged no planned action"
    credential_sentinel.assert_clean()


def test_the_resolved_password_is_registered_before_it_could_be_emitted(
    credential_sentinel: CredentialSentinel,
    double: DatabaseDouble,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[backup_cli.SecretRegistry] = []
    real = cli_root.SecretRegistry

    def spy() -> backup_cli.SecretRegistry:
        created.append(real())
        return created[-1]

    monkeypatch.setattr(cli_root, "SecretRegistry", spy)
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
    with pytest.raises(CredentialsError) as caught:
        _drive(credential_sentinel)
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
    with pytest.raises(CredentialsError) as caught:
        _drive(credential_sentinel, BackupScope.DATABASE)
    assert caught.value.reason == "ValueError"
    credential_sentinel.add_exception("backup driver", caught.value)
    argv = ["--log-level", "DEBUG", "backup", "--wp-dir", str(credential_sentinel.site)]
    assert main(argv) != 0
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
    names = watched_names(verify_cli, "run_verify", "drive_verify")
    assert {"storage_target_from_cli", "verify_archive_view"} <= names
    for name in names:
        credential_sentinel.watch(verify_cli, name)
    root = watched_names(cli_root, *ROOT_DRIVERS)
    assert {"SecretRegistry", "report_failure"} <= root
    for name in root:
        credential_sentinel.watch(cli_root, name)
    debug = ["--log-level", "DEBUG", "verify", "--archive"]
    assert main([*debug, str(archive)]) == 0
    missing = credential_sentinel.workdir / "absent.tar.gz"
    assert main([*debug, str(missing)]) != 0
    out = capsys.readouterr()
    credential_sentinel.add("verify stdout", out.out)
    credential_sentinel.add("verify stderr", out.err)
    request = verify_cli.VerifyRequest(
        archive=backup_cli.storage_target_from_cli(str(missing))
    )
    with pytest.raises(BackupError) as caught:
        verify_cli.drive_verify(request, ctx=None)
    credential_sentinel.add_exception("verify driver", caught.value)
    credential_sentinel.assert_clean()


def test_the_watched_set_is_derived_and_covers_the_credential_path() -> None:
    assert {"SecretRegistry", "report_failure"} <= watched_names(
        cli_root, *ROOT_DRIVERS
    )
    names = watched_names(backup_cli, *BACKUP_DRIVERS)
    assert {
        "site_descriptor_from_wp_config",
        "wordpress_source",
        "real_adapters",
        "execute_backup_plan",
    } <= names
    assert "Path" not in names  # not a package callable
    assert "WpDatabase" in watched_names(adapter_sets, "real_adapters")
    assert {
        "resolve_passphrase",
        "PassphraseCipher",
        "decrypt_payload",
        "verify_archive_view",
    } <= watched_names(verify_cli, *VERIFY_DRIVERS)


def test_a_failed_transfer_emits_no_credential(
    credential_sentinel: CredentialSentinel,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stderr = f"mysqldump: error near {credential_sentinel.password}"
    monkeypatch.setattr(wpdb, "dump", DatabaseDouble(transfer=stderr))
    code, _ = _backup(credential_sentinel, capsys, mode="db")
    assert code != 0
    with pytest.raises(DumpFailedError) as caught:
        _drive(credential_sentinel, BackupScope.DATABASE)
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


def _tamper(payload: bytes) -> bytes:
    flipped = bytearray(payload)
    flipped[len(flipped) // 2] ^= 0x01
    return bytes(flipped)


@pytest.mark.parametrize("credential_sentinel", [{"passphrase": True}], indirect=True)
@pytest.mark.usefixtures("fake_age")
def test_verify_authenticate_emits_no_passphrase(
    credential_sentinel: CredentialSentinel,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BL-E002-060: the passphrase reaches no argv, log, error or output.

    Success, a damaged (forged) payload, a wrong passphrase on the library
    path and a plain Archive, at DEBUG, with every package callable the verify
    drivers reach spied on except the passphrase sources.
    """
    passphrase = credential_sentinel.passphrase
    assert passphrase is not None
    workdir = credential_sentinel.workdir
    sealed = seal_archive(workdir, passphrase)
    forged = forge_archive(sealed, workdir / "forged.tar.gz", _tamper)
    plain = forge_archive(
        seal_archive(workdir, None),
        workdir / "plain-forged.tar.gz",
        backdoor_plain_payload,
    )
    monkeypatch.setattr(verify_cli, "terminal_prompt", lambda: None)
    names = watched_names(verify_cli, *VERIFY_DRIVERS)
    sources = {name for name in names if takes_a_passphrase(verify_cli, name)}
    assert sources == {
        "drive_verify",
        "_authenticate",
        "resolve_passphrase",
        "PassphraseCipher",
    }
    for name in names - sources:
        credential_sentinel.watch(verify_cli, name)
    for name in watched_names(cli_root, *ROOT_DRIVERS):
        credential_sentinel.watch(cli_root, name)
    debug = ["--log-level", "DEBUG", "--log-format", "json", "verify"]
    runs = [
        ([*debug, "--archive", str(sealed), "--authenticate"], 0),
        ([*debug, "--archive", str(forged), "--authenticate"], 8),
        ([*debug, "--archive", str(plain), "--authenticate"], 2),
    ]
    for argv, expected in runs:
        credential_sentinel.add("argv", repr(argv))
        assert main(argv) == expected
    out = capsys.readouterr()
    assert '"authenticated": true' in out.err, "the pipeline wrote no pass"
    credential_sentinel.add("verify stdout", out.out)
    credential_sentinel.add("verify stderr", out.err)
    request = verify_cli.VerifyRequest(
        archive=backup_cli.storage_target_from_cli(str(sealed)), authenticate=True
    )
    for wrong in (f"{passphrase}-not", None):
        with pytest.raises(BackupError) as caught:
            verify_cli.drive_verify(
                request,
                ctx=None,
                passphrase=wrong,
                environ={"L3IO_WP_BACKUP_PASSPHRASE": f"x{passphrase}"},
            )
        credential_sentinel.add_exception("verify --authenticate driver", caught.value)
    credential_sentinel.assert_clean()


@pytest.mark.parametrize("credential_sentinel", [{"passphrase": True}], indirect=True)
@pytest.mark.usefixtures("fake_age")
@pytest.mark.parametrize("plant", ["error from the cipher", "spied argument"])
def test_a_passphrase_leak_on_the_authenticate_path_is_caught(
    credential_sentinel: CredentialSentinel,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    plant: str,
) -> None:
    """The case above examines the new path: a leak planted inside it fails."""
    from l3io.wp.backup.adapters import crypto

    passphrase = credential_sentinel.passphrase
    assert passphrase is not None
    sealed = seal_archive(credential_sentinel.workdir, passphrase)
    monkeypatch.setattr(verify_cli, "terminal_prompt", lambda: None)
    request = verify_cli.VerifyRequest(
        archive=backup_cli.storage_target_from_cli(str(sealed)), authenticate=True
    )
    if plant == "error from the cipher":

        def leaky(self: crypto.PassphraseCipher, *_: object) -> None:
            raise LookupError(self._passphrase)

        monkeypatch.setattr(crypto.PassphraseCipher, "decrypt", leaky)
        with pytest.raises(LookupError) as raised:
            verify_cli.drive_verify(request, ctx=None, environ=os.environ)
        credential_sentinel.add_exception("verify driver", raised.value)
    else:
        monkeypatch.setattr(
            crypto.PassphraseCipher, "__repr__", lambda self: self._passphrase
        )
        credential_sentinel.watch(verify_cli, "decrypt_payload")
        argv = ["--log-level", "DEBUG", "verify", "--archive", str(sealed)]
        assert main([*argv, "--authenticate"]) == 0
        credential_sentinel.add("verify stderr", capsys.readouterr().err)
    with pytest.raises(AssertionError) as caught:
        credential_sentinel.assert_clean()
    assert passphrase not in str(caught.value)


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
# The E002-S02-009 adapters: the config writer and the database's other half
# --------------------------------------------------------------------------


def test_the_config_writer_emits_no_credential(
    credential_sentinel: CredentialSentinel,
) -> None:
    """Reads, rewrites and replaces ``wp-config.php`` holding the sentinel."""
    from l3io.wp.backup.adapters.config_writer import WpConfigWriter
    from l3io.wp.backup.adapters.redaction import SecretRegistry
    from l3io.wp.backup.domain.errors import ConfigWriteFailedError

    s = credential_sentinel
    writer = WpConfigWriter(SecretRegistry())
    s.add_repr("writer", writer)
    read = writer.read_constants(s.config, ["DB_NAME", "DB_USER"])
    s.add_repr("read_constants result", read)
    relocated = f"{s.password}-relocated"
    s.add_repr(
        "set_constants result",
        writer.set_constants(s.config, {"DB_PASSWORD": relocated, "DB_NAME": "moved"}),
    )
    replacement = s.workdir / "replacement-wp-config.php"
    write_wp_config(replacement, password="replacement-password-0")
    s.add_repr(
        "replace_file result",
        writer.replace_file(s.config, replacement, keep={"DB_PASSWORD": s.password}),
    )
    s.add_repr("save_copy result", writer.save_copy(s.config, s.workdir / "saved.php"))
    with pytest.raises(ConfigWriteFailedError) as caught:
        writer.set_constants(
            s.config,
            {"DB_PASSWORD": s.password, "DB_NAME": object()},  # type: ignore[dict-item]
        )
    s.add_exception("config writer", caught.value)
    s.assert_clean()


def test_the_database_admin_and_load_paths_emit_no_credential(
    credential_sentinel: CredentialSentinel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Survey, ensure, load and drop over doubles that resolve both sources.

    The administrator's password is the sentinel; the WordPress account's
    comes from the sentinel's ``wp-config.php``. The last call fails at
    authentication, as database would.
    """
    from l3io.wp.backup.adapters.database import (
        WpDatabase,
        admin_source,
        site_descriptor_from_wp_config,
    )
    from l3io.wp.backup.adapters.redaction import SecretRegistry
    from l3io.wp.backup.domain.credentials import ExplicitCredentialSpec

    s = credential_sentinel
    secrets = SecretRegistry()
    descriptor, wordpress = site_descriptor_from_wp_config(s.site, config_file=s.config)
    admin = admin_source(
        ExplicitCredentialSpec("root", s.password), environ={}, secrets=secrets
    )
    database = WpDatabase(wordpress, secrets=secrets, admin=admin)
    location = descriptor.database_location()
    verdict = wpdb.Verdict("x", ())

    def resolving(result: object) -> object:
        def double(connection: object, *sources: object, **_: object) -> object:
            for source in sources:
                if hasattr(source, "resolve"):
                    source.resolve()
            return result

        return double

    survey = wpdb.Survey(None, False, 0, 0, False, None, None, None)
    monkeypatch.setattr(wpdb, "survey", resolving(survey))
    ensure = wpdb.EnsureResult(True, True, True, True, verdict)
    monkeypatch.setattr(wpdb, "ensure", resolving(ensure))
    loaded = wpdb.TransferResult(3, 1.0, "mysql", None, verdict)
    monkeypatch.setattr(wpdb, "load", resolving(loaded))
    for name in ("survey", "ensure", "load"):
        s.watch(wpdb, name)
    s.add_repr("database", database)
    s.add_repr(
        "survey",
        database.survey_target(location, table_prefix="wp_", wordpress_user="wp"),
    )
    s.add_repr("ensure", database.ensure_database(location))
    s.add_repr("load", database.load_dump(location, s.workdir / "dump.sql"))

    def refused(connection: object, admin: object, **_: object) -> object:
        admin.resolve()  # type: ignore[attr-defined]
        msg = "the account 'root' was rejected"
        raise wpdb.AuthenticationFailedError(msg)

    monkeypatch.setattr(wpdb, "drop_database", refused)
    with pytest.raises(CredentialsError) as caught:
        database.drop_database(location, confirm=location.name)
    s.add_exception("drop_database", caught.value)
    s.assert_clean()


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
    with pytest.raises(CredentialsError) as caught:
        _drive(credential_sentinel, BackupScope.DATABASE)
    credential_sentinel.add_exception("backup driver", caught.value)
    credential_sentinel.assert_clean()
