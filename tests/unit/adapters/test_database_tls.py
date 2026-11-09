"""Owner decision 56 (amending BL-E002-114's TLS policy): TLS is opt-in.

A recording ``Connection`` factory (database's own ``Connection``, wrapped so
each call's ``tls`` is kept) shows what ``WpDatabase._connection`` builds: no
TLS requirement by default, for every host and either account, as WordPress
connects; ``--db-tls`` raises it to the mode asked for, for every host, local
ones included; ``--db-tls-ca`` needs a verifying mode. Both roots bind it.
Each database TLS error maps to its own package class and exit code, without
database's text.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from l3io.wp.backup.adapters.database import (
    DB_TLS_MODES,
    INVALID_DATABASE_NAME,
    INVALID_DB_HOST,
    TLS_SETTINGS_UNUSABLE,
    DatabaseAuthPluginRefusedError,
    DatabaseTlsHandshakeError,
    DatabaseTlsVerificationError,
    WpDatabase,
    check_db_tls,
    database_location_from_db_host,
)
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.cli.adapter_sets import real_adapters
from l3io.wp.backup.cli.parser import build_parser
from l3io.wp.backup.domain.database import DatabaseLocation
from l3io.wp.backup.domain.errors import UsageError
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.domain.restore import RestoreMode
from l3io.wp.backup.ports.database import DbTls
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


def _database(db_tls: DbTls | None) -> WpDatabase:
    return WpDatabase(
        wpdb.Explicit("wp", "not-a-real-secret-0"),
        secrets=SecretRegistry(),
        admin=wpdb.Explicit("admin", "not-a-real-secret-1"),
        db_tls=db_tls,
    )


def _connect(database: WpDatabase, host: str) -> None:
    database._connection(DatabaseLocation(host=host, port=3306, name="wp"))  # noqa: SLF001


def test_the_local_host_scope_is_databases_own() -> None:
    assert {"localhost", "127.0.0.1", "::1"} <= set(LOCAL_HOSTS)


@pytest.mark.parametrize("host", [REMOTE, *LOCAL_HOSTS])
@pytest.mark.parametrize("tls", [None, DbTls()], ids=["unbound", "no-mode"])
def test_no_tls_is_required_by_default_for_any_host(
    built: list[object], host: str, tls: DbTls | None
) -> None:
    _connect(_database(tls), host)
    assert [t.mode for t in built] == [wpdb.TlsMode.OFF]  # type: ignore[attr-defined]


@pytest.mark.parametrize("mode", DB_TLS_MODES)
@pytest.mark.parametrize("host", [REMOTE, "localhost"])
def test_db_tls_requires_its_mode_for_every_host(
    built: list[object], mode: str, host: str
) -> None:
    _connect(_database(DbTls(mode=mode)), host)
    wanted = wpdb.TlsMode(mode.replace("-", "_"))  # type: ignore[attr-defined]
    assert [t.mode for t in built] == [wanted]  # type: ignore[attr-defined]


@pytest.mark.parametrize("mode", ["verify-ca", "verify-identity"])
def test_a_ca_file_reaches_the_settings(
    built: list[object], tmp_path: Path, mode: str
) -> None:
    ca = tmp_path / "ca.pem"
    _connect(_database(DbTls(mode=mode, ca_file=ca)), REMOTE)
    assert [t.ca_file for t in built] == [ca]  # type: ignore[attr-defined]


@pytest.mark.parametrize("mode", [None, "required"])
def test_a_ca_file_without_a_verifying_mode_is_usage_before_any_connection(
    built: list[object], tmp_path: Path, mode: str | None
) -> None:
    with pytest.raises(UsageError, match="--db-tls-ca needs") as caught:
        _connect(_database(DbTls(mode=mode, ca_file=tmp_path / "ca.pem")), REMOTE)
    assert caught.value.exit_code is ExitCode.USAGE
    assert built == []


class _Tracer:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(name)


def test_unbound_adapters_require_no_tls(built: list[object]) -> None:
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
    database = _database(DbTls(mode=None, ca_file=None))
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
    database = _database(DbTls(mode=None, ca_file=None))
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
    tls = DbTls(mode="verify-identity", ca_file=None)
    rig.drive(db_tls=tls, mode=RestoreMode.DATABASE)
    assert rig.adapter_calls[-1]["db_tls"] is tls


def test_the_restore_root_refuses_unusable_tls_before_the_bind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path, monkeypatch)
    with pytest.raises(UsageError, match="--db-tls-ca needs"):
        rig.drive(db_tls=DbTls(mode="required", ca_file=tmp_path / "ca.pem"))
    assert rig.adapter_calls == []
    assert rig.executions == []


def test_a_restore_request_holds_a_db_tls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path, monkeypatch)
    assert rig.request().db_tls == DbTls()
    with pytest.raises(TypeError, match="DbTls"):
        dataclasses.replace(rig.request(), db_tls=None)


# --- ADV-S02-11: a UsageError never forwards database's message -----------------


def test_unusable_tls_settings_are_a_fixed_usage_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(**_: object) -> object:
        raise wpdb.InvalidArgumentsError(DATABASE_TEXT)

    monkeypatch.setattr(wpdb, "TlsSettings", refuse)
    with pytest.raises(UsageError) as caught:
        check_db_tls(DbTls(mode="verify-identity", ca_file=None))
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


def test_the_tls_copies_are_databases_tls_modes() -> None:
    """L-5 (BL-E003-063): the hand-kept tuples match database's ``TlsMode``.

    They cannot be derived at import (the ``database`` extra is optional), so
    this test is the derivation: a mode database adds or renames fails here.
    """
    library = tuple(mode.value for mode in wpdb.TlsMode)  # type: ignore[attr-defined]
    assert library[0] == "off"  # what no --db-tls means
    assert tuple(m.replace("-", "_") for m in DB_TLS_MODES) == library[1:]
    base = ["restore", "--wp-dir", "/srv/site", "--archive", "a.tar.gz"]
    for flag in ("--db-tls", "--admin-tls"):
        for mode in DB_TLS_MODES:
            parsed = build_parser().parse_args([*base, flag, mode])
            assert getattr(parsed, flag.lstrip("-").replace("-", "_")) == mode
        with pytest.raises(SystemExit):
            build_parser().parse_args([*base, flag, "off"])
