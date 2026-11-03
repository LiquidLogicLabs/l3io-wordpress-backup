"""ADR-0015/AD-29 — ``WpDatabase``'s restore half, through database only.

The ``integration`` tests need the CI ``db`` service (MySQL 8.x / MariaDB),
reached through ``DB_HOST`` and ``DB_PASSWORD`` as ``root``, the
administrator. Each test owns a database and WordPress account whose names
are unique to it, and removes both afterwards. They skip when ``DB_HOST`` is
unset; ``make ci`` runs them.

The remaining tests replace database's functions with doubles: how each
failure is re-raised (AD-14), that ``load`` is never handed ``replace`` or a
decryption argument, and what each command records (AD-10).
"""

from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from structlog.testing import capture_logs

wpdb = pytest.importorskip("l3io.wp.database")

from l3io.wp.backup.adapters.database import WpDatabase, admin_source  # noqa: E402
from l3io.wp.backup.adapters.redaction import SecretRegistry  # noqa: E402
from l3io.wp.backup.domain.credentials import ExplicitCredentialSpec  # noqa: E402
from l3io.wp.backup.domain.database import (  # noqa: E402
    DatabaseLocation,
    DatabaseTargetState,
    EnsureOutcome,
    LoadResult,
)
from l3io.wp.backup.domain.errors import (  # noqa: E402
    AdminCredentialsRequiredError,
    CredentialsError,
    DatabaseRefusedError,
    DatabaseUnreachableError,
    LoadFailedError,
    MissingExtraError,
    ProvisioningFailedError,
)

live = pytest.mark.skipif(
    "DB_HOST" not in os.environ, reason="needs the CI database (DB_HOST unset)"
)

#: A minimal WordPress-shaped Dump: ``wp_options`` holding ``db_version``.
DUMP = (
    b"CREATE TABLE `wp_options` (\n"
    b"  `option_id` bigint unsigned NOT NULL AUTO_INCREMENT,\n"
    b"  `option_name` varchar(191) NOT NULL DEFAULT '',\n"
    b"  `option_value` longtext NOT NULL,\n"
    b"  `autoload` varchar(20) NOT NULL DEFAULT 'yes',\n"
    b"  PRIMARY KEY (`option_id`)\n"
    b");\n"
    b"INSERT INTO `wp_options` VALUES (1,'db_version','57155','yes');\n"
)


# --------------------------------------------------------------------------
# Against the CI database
# --------------------------------------------------------------------------


@dataclass
class Target:
    """A restore target unique to one test, and the adapter bound to it."""

    database: WpDatabase
    location: DatabaseLocation
    user: str
    secrets: SecretRegistry
    wordpress_password: str


@pytest.fixture
def target() -> Iterator[Target]:
    host = os.environ["DB_HOST"]
    port = int(os.environ.get("DB_PORT", "3306"))
    suffix = uuid.uuid4().hex[:10]
    name, user = f"wpr_{suffix}", f"wpr_{suffix}"
    admin_password = os.environ["DB_PASSWORD"]
    wordpress_password = f"wp-{uuid.uuid4().hex}"
    secrets = SecretRegistry()
    admin = admin_source(
        ExplicitCredentialSpec(os.environ.get("DB_ADMIN_USER", "root"), admin_password),
        environ={},
        secrets=secrets,
    )
    database = WpDatabase(
        wpdb.Explicit(user, wordpress_password), secrets=secrets, admin=admin
    )
    location = DatabaseLocation(host=host, port=port, name=name)
    yield Target(database, location, user, secrets, wordpress_password)
    # Teardown through database, as the administrator; absent is fine.
    connection = wpdb.Connection(host=host, database=name, port=port)
    root = wpdb.Explicit(os.environ.get("DB_ADMIN_USER", "root"), admin_password)
    wpdb.drop_database(connection, root, confirm=name)
    with contextlib.suppress(wpdb.DatabaseError):  # never created
        wpdb.drop_user(connection, root, user, confirm=user)


def _dump(tmp_path: Path) -> Path:
    path = tmp_path / "dump.sql"
    path.write_bytes(DUMP)
    return path


@pytest.mark.integration
@live
def test_survey_of_an_absent_database(target: Target) -> None:
    state = target.database.survey_target(
        target.location, table_prefix="wp_", wordpress_user=target.user
    )
    assert state == DatabaseTargetState(
        database_exists=False,
        table_count=0,
        is_wordpress=False,
        schema_version=None,
        user_exists=False,
    )


@pytest.mark.integration
@live
def test_ensure_creates_then_reports_nothing_to_do(target: Target) -> None:
    assert target.database.check_ensure(target.location).ready
    first = target.database.ensure_database(target.location)
    assert first.performed
    assert first.created_database
    assert first.created_user
    second = target.database.ensure_database(target.location)
    assert second == EnsureOutcome(
        created_database=False,
        created_user=False,
        password_set=False,
        granted=False,
        warnings=second.warnings,
        performed=True,
    )
    empty = target.database.survey_target(
        target.location, table_prefix="wp_", wordpress_user=target.user
    )
    assert (empty.database_exists, empty.table_count, empty.user_exists) == (
        True,
        0,
        True,
    )
    assert not empty.is_wordpress


@pytest.mark.integration
@live
def test_load_into_an_empty_database_then_a_second_load_is_refused(
    target: Target, tmp_path: Path
) -> None:
    target.database.ensure_database(target.location)
    dump = _dump(tmp_path)
    assert target.database.check_load(target.location, source=dump).ready
    loaded = target.database.load_dump(target.location, dump)
    assert isinstance(loaded, LoadResult)
    assert loaded.performed
    surveyed = target.database.survey_target(
        target.location, table_prefix="wp_", wordpress_user=target.user
    )
    assert surveyed.is_wordpress
    assert surveyed.table_count == 1
    assert surveyed.schema_version == "57155"
    readiness = target.database.check_load(target.location, source=dump)
    assert "database_not_empty" in readiness.blockers
    with pytest.raises(DatabaseRefusedError) as caught:
        target.database.load_dump(target.location, dump)
    assert "database_not_empty" in caught.value.findings
    after = target.database.survey_target(
        target.location, table_prefix="wp_", wordpress_user=target.user
    )
    assert after.table_count == 1  # nothing was loaded over it


@pytest.mark.integration
@live
def test_revoke_then_drop(target: Target) -> None:
    target.database.ensure_database(target.location)
    where, user = target.location, target.user
    assert target.database.check_revoke(where, user=user, confirm=user).ready
    assert target.database.revoke_grants(where, user=user, confirm=user) is True
    assert target.database.check_drop_database(where, confirm=where.name).ready
    assert target.database.drop_database(where, confirm=where.name) is True
    gone = target.database.survey_target(where, table_prefix="wp_", wordpress_user=None)
    assert not gone.database_exists


@pytest.mark.integration
@live
def test_drop_of_an_absent_database_is_false(target: Target) -> None:
    where = target.location
    assert target.database.drop_database(where, confirm=where.name) is False


@pytest.mark.integration
@live
def test_the_wordpress_password_is_registered_by_ensure(target: Target) -> None:
    target.database.ensure_database(target.location)
    password = target.wordpress_password
    assert password not in target.secrets.redact(password)


# --------------------------------------------------------------------------
# Against doubles
# --------------------------------------------------------------------------

WHERE = DatabaseLocation(host="db.invalid", port=3306, name="wptest")


def _verdict(operation: str, *codes: str) -> object:
    findings = tuple(wpdb.Finding(wpdb.Code(c), c) for c in codes)
    return wpdb.Verdict(operation, findings)


@pytest.fixture
def admin_database() -> WpDatabase:
    return WpDatabase(
        wpdb.Explicit("wordpress", "never-shown-wp"),
        secrets=SecretRegistry(),
        admin=wpdb.Explicit("root", "never-shown-admin"),
    )


def _raising(error: BaseException) -> object:
    def double(*_: object, **__: object) -> object:
        raise error

    return double


#: Each restore method, how to call it, and the database function behind it.
CALLS = {
    "survey_target": (
        "survey",
        lambda db: db.survey_target(WHERE, table_prefix="wp_", wordpress_user="u"),
    ),
    "check_ensure": ("check_ensure", lambda db: db.check_ensure(WHERE)),
    "check_load": ("check_load", lambda db: db.check_load(WHERE, source=None)),
    "check_revoke": (
        "check_revoke",
        lambda db: db.check_revoke(WHERE, user="u", confirm="u"),
    ),
    "check_drop_database": (
        "check_drop_database",
        lambda db: db.check_drop_database(WHERE, confirm="wptest"),
    ),
    "ensure_database": ("ensure", lambda db: db.ensure_database(WHERE)),
    "load_dump": ("load", lambda db: db.load_dump(WHERE, Path("/dev/null"))),
    "revoke_grants": (
        "revoke",
        lambda db: db.revoke_grants(WHERE, user="u", confirm="u"),
    ),
    "drop_database": (
        "drop_database",
        lambda db: db.drop_database(WHERE, confirm="wptest"),
    ),
}


def test_the_call_table_covers_every_restore_method() -> None:
    from l3io.wp.backup.ports.database import DatabaseCommand, DatabaseQuery

    members = {
        name
        for protocol in (DatabaseQuery, DatabaseCommand)
        for name in vars(protocol)
        if not name.startswith("_") and callable(vars(protocol)[name])
    } - {"check_dump", "dump_to"}
    assert set(CALLS) == members


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (wpdb.DatabaseUnreachableError("x"), DatabaseUnreachableError),
        (wpdb.AuthenticationFailedError("x"), CredentialsError),
        (wpdb.AccessDeniedError("x"), CredentialsError),
        (wpdb.MissingDependencyError("x", "y"), MissingExtraError),
    ],
)
@pytest.mark.parametrize("method", sorted(CALLS))
def test_each_method_maps_database_errors(
    admin_database: WpDatabase,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    raised: Exception,
    expected: type[Exception],
) -> None:
    function, call = CALLS[method]
    monkeypatch.setattr(wpdb, function, _raising(raised))
    with pytest.raises(expected) as caught:
        call(admin_database)
    assert caught.value.__cause__ is raised
    assert "never-shown" not in str(caught.value)


@pytest.mark.parametrize(
    "method", sorted(m for m in CALLS if m not in {"load_dump", "check_load"})
)
def test_an_administrator_failure_is_a_provisioning_failure(
    admin_database: WpDatabase, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    function, call = CALLS[method]
    error = wpdb.ProvisioningError("never-shown-text", completed=("create_database",))
    monkeypatch.setattr(wpdb, function, _raising(error))
    with pytest.raises(ProvisioningFailedError) as caught:
        call(admin_database)
    assert caught.value.__cause__ is error
    assert "never-shown-text" not in str(caught.value)
    assert caught.value.completed == ("create_database",)


@pytest.mark.parametrize(
    "error",
    [
        wpdb.TransferError("never-shown-text", _verdict("load"), 1, "stderr"),
        wpdb.CorruptDumpError("never-shown-text"),
    ],
)
def test_a_failed_load_is_a_load_failure(
    admin_database: WpDatabase, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    monkeypatch.setattr(wpdb, "load", _raising(error))
    with pytest.raises(LoadFailedError) as caught:
        admin_database.load_dump(WHERE, Path("/dev/null"))
    assert caught.value.__cause__ is error
    assert "never-shown-text" not in str(caught.value)


def test_a_refused_load_is_a_database_refusal_by_code(
    admin_database: WpDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    refused = wpdb.RefusedError(_verdict("load", "database_not_empty"))
    monkeypatch.setattr(wpdb, "load", _raising(refused))
    with pytest.raises(DatabaseRefusedError) as caught:
        admin_database.load_dump(WHERE, Path("/dev/null"))
    assert caught.value.findings == ("database_not_empty",)
    assert caught.value.__cause__ is refused


def test_load_is_never_handed_replace_or_a_decryption_argument(
    admin_database: WpDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def load(
        connection: object, credentials: object, source: object, **kw: object
    ) -> object:
        seen.update(kw, source=source, database=connection.database)  # type: ignore[attr-defined]
        return wpdb.TransferResult(7, 1.5, "mysql", None, _verdict("load"))

    monkeypatch.setattr(wpdb, "load", load)
    result = admin_database.load_dump(WHERE, Path("/dev/null"))
    assert result == LoadResult(bytes=7, duration_ms=1.5, performed=True)
    assert seen == {"source": Path("/dev/null"), "database": "wptest"}


def test_load_needs_no_administrator(monkeypatch: pytest.MonkeyPatch) -> None:
    bare = WpDatabase(wpdb.Explicit("wordpress", "x" * 12), secrets=SecretRegistry())
    monkeypatch.setattr(
        wpdb,
        "load",
        lambda *_, **__: wpdb.TransferResult(0, 0.0, "mysql", None, _verdict("load")),
    )
    assert bare.load_dump(WHERE, Path("/dev/null")).performed
    with pytest.raises(AdminCredentialsRequiredError):
        bare.check_load(WHERE, source=None)


def test_ensure_copies_the_result_with_warning_codes(
    admin_database: WpDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def ensure(
        connection: object, admin: object, wordpress: object, **kw: object
    ) -> object:
        seen.update(kw)
        return wpdb.EnsureResult(
            True, False, True, True, _verdict("ensure", "live_wordpress_site")
        )

    monkeypatch.setattr(wpdb, "ensure", ensure)
    with capture_logs() as logs:
        outcome = admin_database.ensure_database(WHERE)
    assert outcome == EnsureOutcome(
        created_database=True,
        created_user=False,
        password_set=True,
        granted=True,
        warnings=("live_wordpress_site",),
        performed=True,
    )
    assert seen == {"user_host": "%"}
    (record,) = [e for e in logs if e["event"] == "database_ensured"]
    assert record["db_name"] == "wptest"
    assert "duration_ms" in record
    assert record["created_database"] is True


def test_survey_is_answered_by_the_administrator(
    admin_database: WpDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def survey(connection: object, admin: object, **kw: object) -> object:
        seen.update(kw, admin=admin.source)  # type: ignore[attr-defined]
        return wpdb.Survey(
            server=None,
            database_exists=True,
            table_count=12,
            size_bytes=0,
            is_wordpress=True,
            schema_version=wpdb.SchemaVersion(57155),
            user_exists=None,
            user_privileges=None,
        )

    monkeypatch.setattr(wpdb, "survey", survey)
    with capture_logs() as logs:
        state = admin_database.survey_target(
            WHERE, table_prefix="xy_", wordpress_user=None
        )
    assert state == DatabaseTargetState(True, 12, True, "57155", None)
    assert seen == {
        "wordpress_user": None,
        "user_host": "%",
        "table_prefix": "xy_",
        "admin": "explicit",
    }
    (record,) = [e for e in logs if e["event"] == "database_surveyed"]
    assert record["observed_by"] == "administrator"


def test_drop_of_a_live_site_logs_the_warning_and_returns_changed(
    admin_database: WpDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def drop(connection: object, admin: object, **kw: object) -> object:
        seen.update(kw)
        return wpdb.OperationResult(
            False, _verdict("drop_database", "live_wordpress_site")
        )

    monkeypatch.setattr(wpdb, "drop_database", drop)
    with capture_logs() as logs:
        assert admin_database.drop_database(WHERE, confirm="wptest") is False
    assert seen == {"confirm": "wptest"}
    events = {e["event"]: e for e in logs}
    assert events["database_drop_warnings"]["warnings"] == ("live_wordpress_site",)
    assert events["database_dropped"]["changed"] is False


def test_revoke_returns_changed_and_records_it(
    admin_database: WpDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        wpdb,
        "revoke",
        lambda *_, **__: wpdb.OperationResult(True, _verdict("revoke")),
    )
    with capture_logs() as logs:
        assert admin_database.revoke_grants(WHERE, user="u", confirm="u") is True
    (record,) = [e for e in logs if e["event"] == "grants_revoked"]
    assert record["changed"] is True


@pytest.mark.parametrize("method", sorted(set(CALLS) - {"load_dump"}))
def test_without_an_administrator_nothing_is_called(
    monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    function, call = CALLS[method]
    monkeypatch.setattr(wpdb, function, _raising(AssertionError("reached database")))
    bare = WpDatabase(wpdb.Explicit("wordpress", "x" * 12), secrets=SecretRegistry())
    with pytest.raises(AdminCredentialsRequiredError):
        call(bare)


def test_an_unobtainable_wordpress_credential_names_the_wordpress_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review F1: not the administrator's label, and database is not called."""

    class Absent:
        source = "wp-config:/site/wp-config.php"

        def resolve(self) -> object:
            msg = "DB_PASSWORD is not defined"
            raise wpdb.CredentialsAbsentError(msg)

    monkeypatch.setattr(wpdb, "ensure", _raising(AssertionError("reached database")))
    database = WpDatabase(
        Absent(),  # type: ignore[arg-type]
        secrets=SecretRegistry(),
        admin=wpdb.Explicit("root", "never-shown-admin"),
    )
    with pytest.raises(CredentialsError) as caught:
        database.ensure_database(WHERE)
    assert caught.value.source == "wp-config:/site/wp-config.php"
