"""BL-E002-114, the TLS half — administrator connections to a non-local host.

A recording ``Connection`` factory (database's own ``Connection``, wrapped so
each call's ``tls`` is kept) shows what ``WpDatabase._connection`` builds:
``VERIFY_IDENTITY`` for a remote host, ``OFF`` for each of database's local
hosts, an override that may raise and never lower the policy, and plaintext
for the backup root, which binds no ``admin_tls``. Each database TLS error
maps to its own package class and exit code, without database's text.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from l3io.wp.backup.adapters.database import (
    INVALID_DATABASE_NAME,
    INVALID_DB_HOST,
    TLS_SETTINGS_UNUSABLE,
    AdminTls,
    DatabaseAuthPluginRefusedError,
    DatabaseTlsHandshakeError,
    DatabaseTlsVerificationError,
    WpDatabase,
    check_admin_tls,
    database_location_from_db_host,
)
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.cli.adapter_sets import real_adapters
from l3io.wp.backup.domain.database import DatabaseLocation
from l3io.wp.backup.domain.errors import UsageError
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.domain.restore import RestoreMode
from tests.restore_root_rig import Rig

wpdb = pytest.importorskip("l3io.wp.database")

#: database's own local hosts: the scope of the "OFF" case.
LOCAL_HOSTS = sorted(wpdb.tls.DEFAULT_LOCAL_HOSTS)
REMOTE = "db.example.invalid"
#: Text only database's exception carries; it must never reach ours.
DATABASE_TEXT = "database-library-text-e003-tls"


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Every ``tls`` the adapter passed to ``Connection``, in order."""
    seen: list[object] = []
    real = wpdb.Connection

    def recording(**kwargs: object) -> object:
        seen.append(kwargs["tls"])
        return real(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(wpdb, "Connection", recording)
    return seen


def _database(admin_tls: AdminTls | None) -> WpDatabase:
    return WpDatabase(
        wpdb.Explicit("wp", "not-a-real-secret-0"),
        secrets=SecretRegistry(),
        admin=wpdb.Explicit("admin", "not-a-real-secret-1"),
        admin_tls=admin_tls,
    )


def _connect(database: WpDatabase, host: str) -> None:
    database._connection(DatabaseLocation(host=host, port=3306, name="wp"))  # noqa: SLF001


def test_the_local_host_scope_is_databases_own() -> None:
    assert {"localhost", "127.0.0.1", "::1"} <= set(LOCAL_HOSTS)


def test_a_remote_host_builds_verify_identity(built: list[object]) -> None:
    _connect(_database(AdminTls(mode=None, ca_file=None)), REMOTE)
    (tls,) = built
    assert tls.mode is wpdb.TlsMode.VERIFY_IDENTITY  # type: ignore[attr-defined]
    assert tls.ca_file is None  # type: ignore[attr-defined]  # the system store


@pytest.mark.parametrize("host", LOCAL_HOSTS)
def test_a_local_host_builds_off(built: list[object], host: str) -> None:
    _connect(_database(AdminTls(mode=None, ca_file=None)), host)
    assert [t.mode for t in built] == [wpdb.TlsMode.OFF]  # type: ignore[attr-defined]


def test_an_override_below_the_policy_is_usage_before_any_connection(
    built: list[object],
) -> None:
    with pytest.raises(UsageError, match="below the policy") as caught:
        _connect(_database(AdminTls(mode="verify-ca", ca_file=None)), REMOTE)
    assert caught.value.exit_code is ExitCode.USAGE
    assert built == []


def test_required_on_localhost_builds_required(built: list[object]) -> None:
    _connect(_database(AdminTls(mode="required", ca_file=None)), "localhost")
    assert [t.mode for t in built] == [wpdb.TlsMode.REQUIRED]  # type: ignore[attr-defined]


def test_a_ca_file_reaches_the_settings(built: list[object], tmp_path: Path) -> None:
    ca = tmp_path / "ca.pem"
    _connect(_database(AdminTls(mode=None, ca_file=ca)), REMOTE)
    assert [t.ca_file for t in built] == [ca]  # type: ignore[attr-defined]


def test_a_ca_file_for_a_plaintext_local_host_is_usage(tmp_path: Path) -> None:
    with pytest.raises(UsageError, match="--admin-tls-ca"):
        _connect(
            _database(AdminTls(mode=None, ca_file=tmp_path / "ca.pem")), "localhost"
        )


class _Tracer:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(name)


def test_the_backup_roots_connections_stay_off(built: list[object]) -> None:
    adapters = real_adapters(
        wordpress_source=wpdb.Explicit("wp", "not-a-real-secret-0"),
        secrets=SecretRegistry(),
        tracer=_Tracer(),  # type: ignore[arg-type]
    )
    for half in (adapters.database_query, adapters.restore_database_query):
        assert isinstance(half, WpDatabase)
        _connect(half, REMOTE)
    assert [t.mode for t in built] == [wpdb.TlsMode.OFF] * 2  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("raised", "mapped", "code"),
    [
        (wpdb.TlsHandshakeError, DatabaseTlsHandshakeError, ExitCode.UNREACHABLE),
        (wpdb.TlsVerificationError, DatabaseTlsVerificationError, ExitCode.REFUSED),
    ],
    ids=["handshake", "verification"],
)
@pytest.mark.parametrize("call", ["check_dump", "survey_target"])
def test_each_tls_error_maps_to_its_own_class(
    monkeypatch: pytest.MonkeyPatch,
    raised: type[Exception],
    mapped: type[Exception],
    code: ExitCode,
    call: str,
) -> None:
    def fail(*args: object, **kwargs: object) -> object:
        raise raised(DATABASE_TEXT)

    monkeypatch.setattr(wpdb, "check_dump" if call == "check_dump" else "survey", fail)
    database = _database(AdminTls(mode=None, ca_file=None))
    location = DatabaseLocation(host=REMOTE, port=3306, name="wp")

    def run() -> object:
        if call == "check_dump":
            return database.check_dump(location)
        return database.survey_target(location, table_prefix="wp_", wordpress_user=None)

    with pytest.raises(mapped) as caught:
        run()
    assert caught.value.exit_code is code  # type: ignore[attr-defined]
    assert DATABASE_TEXT not in str(caught.value)
    assert REMOTE in str(caught.value)
    assert type(caught.value) is mapped


@pytest.mark.parametrize(
    ("plugin", "shown"),
    [
        ("mysql_clear_password", "mysql_clear_password"),
        ("dialog", "dialog"),
        ("evil; rm -rf /\n\x1b[2J", "unnamed"),
    ],
    ids=["cleartext", "dialog", "hostile-name"],
)
@pytest.mark.parametrize("call", ["check_dump", "survey_target", "load_dump"])
def test_a_refused_auth_plugin_is_refused_naming_only_the_plugin(
    monkeypatch: pytest.MonkeyPatch, call: str, plugin: str, shown: str
) -> None:
    """RT-S02-003: database 0.3.2 refuses a password-exposing plugin; REFUSED."""

    def fail(*args: object, **kwargs: object) -> object:
        raise wpdb.AuthPluginRefusedError(plugin, DATABASE_TEXT)  # type: ignore[attr-defined]

    target = {"check_dump": "check_dump", "survey_target": "survey"}.get(call, "load")
    monkeypatch.setattr(wpdb, target, fail)
    database = _database(AdminTls(mode=None, ca_file=None))
    location = DatabaseLocation(host=REMOTE, port=3306, name="wp")
    calls = {
        "check_dump": lambda: database.check_dump(location),
        "survey_target": lambda: database.survey_target(
            location, table_prefix="wp_", wordpress_user=None
        ),
        "load_dump": lambda: database.load_dump(location, Path("/dev/null")),
    }
    with pytest.raises(DatabaseAuthPluginRefusedError) as caught:
        calls[call]()
    assert caught.value.exit_code is ExitCode.REFUSED
    assert caught.value.plugin == shown
    text = str(caught.value)
    assert DATABASE_TEXT not in text
    assert REMOTE in text
    assert shown in text
    if shown == "unnamed":
        assert plugin not in text


# --------------------------------------------------------------------------
# Through the restore root
# --------------------------------------------------------------------------


def test_the_restore_root_binds_its_admin_tls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path, monkeypatch)
    tls = AdminTls(mode="verify-identity", ca_file=None)
    rig.drive(admin_tls=tls, mode=RestoreMode.DATABASE)
    assert rig.adapter_calls[-1]["admin_tls"] is tls


def test_the_restore_root_refuses_a_lowered_tls_before_the_bind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path, monkeypatch)  # the target's DB_HOST is remote: db.target
    with pytest.raises(UsageError, match="below the policy"):
        rig.drive(admin_tls=AdminTls(mode="required", ca_file=None))
    assert rig.adapter_calls == []
    assert rig.executions == []


def test_a_library_caller_cannot_drop_the_tls_policy_with_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M-3: ``admin_tls=None`` would bind the backup root's plaintext connections."""
    rig = Rig(tmp_path, monkeypatch)
    with pytest.raises(TypeError, match="AdminTls"):
        rig.drive(admin_tls=None, mode=RestoreMode.DATABASE)
    with pytest.raises(TypeError, match="AdminTls"):
        dataclasses.replace(rig.request(), admin_tls=None)
    assert rig.adapter_calls == []
    assert rig.executions == []


# --- ADV-S02-11: a UsageError never forwards database's message -----------------


def test_unusable_tls_settings_are_a_fixed_usage_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(**_: object) -> object:
        raise wpdb.InvalidArgumentsError(DATABASE_TEXT)

    monkeypatch.setattr(wpdb, "TlsSettings", refuse)
    with pytest.raises(UsageError) as caught:
        check_admin_tls(AdminTls(mode="verify-identity", ca_file=None), REMOTE)
    assert str(caught.value) == TLS_SETTINGS_UNUSABLE
    assert DATABASE_TEXT not in str(caught.value)


#: A port database refuses, typed by an operator: never echoed (L-4).
TYPED_PORT = "port-typed-e003-l4"


@pytest.mark.parametrize(
    ("db_host", "name", "expected"),
    [
        (f"db.example.invalid:{TYPED_PORT}", "wordpress", INVALID_DB_HOST),
        ("db.example.invalid:70000", "wordpress", INVALID_DB_HOST),
        ("", "wordpress", INVALID_DB_HOST),
        ("db.example.invalid", f"bad`{TYPED_PORT}", INVALID_DATABASE_NAME),
    ],
    ids=["port-not-integer", "port-out-of-range", "empty-host", "bad-name"],
)
def test_an_unparseable_db_host_is_a_fixed_usage_error(
    db_host: str, name: str, expected: str
) -> None:
    with pytest.raises(UsageError) as caught:
        database_location_from_db_host(db_host, name)
    assert str(caught.value) == expected
    assert TYPED_PORT not in str(caught.value)
    assert str(caught.value.__cause__) not in str(caught.value)
