"""FR-9, AD-28, AD-29 — three servers, three accurate reports (E003-S02-004).

A restore through the real ``check_staged``/``load_staged`` slots
(``tests/integration/live_restore.py``) onto:

- (a) a fresh server — no database, no WordPress account: the report says
  both were created;
- (b) an account that exists without its database: the database was
  created, the account was not (AD-29's reachable case);
- (c) a populated server: refused by FR-7 without ``--force``, naming the
  live database the administrator surveyed; with ``--force`` alone the load
  went into staging, and the report says restored with the live database
  retained;
- (d) a truncated Dump member: the Run raises, exits non-zero, and the live
  database's ``CHECKSUM TABLE`` is unchanged;
- (e) AD-28 by reachability: the restored account sees its own database and
  cannot open a neighbour whose name differs only where ``_`` would match
  as a pattern.

The administrator is a throwaway account whose password is
``credential_sentinel.admin_password``; existence is read from its survey,
never from the WordPress account's login. Every database and account is
uniquely named and dropped by CI's own connection.

Needs the CI ``db`` service (``DB_HOST``/``DB_PASSWORD``); skips without it.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

pytest.importorskip("l3io.wp.database")

from l3io.wp.backup.adapters.database import WpDatabase, admin_source
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.cli.exit import exit_code_for
from l3io.wp.backup.core.restore import (
    NO_RELOCATION,
    NonEmptyTargetError,
    staging_name,
)
from l3io.wp.backup.domain.credentials import ExplicitCredentialSpec
from l3io.wp.backup.domain.database import DatabaseLocation, DatabaseTargetState
from l3io.wp.backup.domain.errors import BackupError
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.domain.restore import Override, OverrideSet, RestoreMode
from tests.conftest import CredentialSentinel
from tests.integration import live_restore
from tests.integration.live_restore import (
    PREFIX,
    LiveSite,
    RestoreSpec,
    build_archive,
    host,
    port,
    read_keys,
    replace_spec,
    restore,
    root_connection,
    spec_for,
)

pymysql = pytest.importorskip("pymysql")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        "DB_HOST" not in os.environ, reason="needs the CI database (DB_HOST unset)"
    ),
]


@dataclass
class Server:
    """A Site Root over a throwaway database name, its account and an admin."""

    site: LiveSite
    admin_user: str
    admin_password: str = field(repr=False)

    def spec(self, archive: Path, overrides: OverrideSet | None = None) -> RestoreSpec:
        spec = spec_for(
            self.site, archive, RestoreMode.DATABASE, relocation=NO_RELOCATION
        )
        return replace_spec(
            spec,
            admin_user=self.admin_user,
            admin_password=self.admin_password,
            overrides=OverrideSet.none() if overrides is None else overrides,
        )

    def survey(self, name: str) -> DatabaseTargetState:
        """The administrator's view of ``name`` and the site's account (AD-29)."""
        from l3io.wp import database as wpdb

        secrets = SecretRegistry()
        admin = admin_source(
            ExplicitCredentialSpec(self.admin_user, self.admin_password),
            environ={},
            secrets=secrets,
        )
        database = WpDatabase(
            wpdb.Explicit(self.site.user, self.site.password),
            secrets=secrets,
            admin=admin,
        )
        return database.survey_target(
            DatabaseLocation(host=host(), port=port(), name=name),
            table_prefix=PREFIX,
            wordpress_user=self.site.user,
        )


def _server(
    s: CredentialSentinel, *, user: str, seeded: bool, admin_user: str
) -> Server:
    site = LiveSite(
        site_root=s.site,
        config=s.config,
        database=f"wpr_{uuid.uuid4().hex[:10]}",
        user=user,
        password=s.password,
        work=s.workdir,
    )
    if seeded:
        site.seed()
    site.write_config()
    return Server(site=site, admin_user=admin_user, admin_password=s.admin_password)


@pytest.fixture
def admin_account(credential_sentinel: CredentialSentinel) -> Iterator[str]:
    """A throwaway administrator (``WITH GRANT OPTION``) with a sentinel password."""
    user = f"wpa_{uuid.uuid4().hex[:12]}"
    with root_connection().cursor() as cursor:
        cursor.execute(
            "CREATE USER %s@'%%' IDENTIFIED BY %s",
            (user, credential_sentinel.admin_password),
        )
        cursor.execute(
            "GRANT ALL PRIVILEGES ON *.* TO %s@'%%' WITH GRANT OPTION", (user,)
        )
    try:
        yield user
    finally:
        with root_connection().cursor() as cursor:
            cursor.execute("DROP USER IF EXISTS %s@'%%'", (user,))


@pytest.fixture
def fresh(
    credential_sentinel: CredentialSentinel, admin_account: str
) -> Iterator[Server]:
    """(a): neither the database nor the WordPress account exists."""
    user = f"wpf_{uuid.uuid4().hex[:12]}"
    server = _server(
        credential_sentinel, user=user, seeded=False, admin_user=admin_account
    )
    server.site.extra_users.append(user)
    try:
        yield server
    finally:
        server.site.teardown()


@pytest.fixture
def account_only(
    credential_sentinel: CredentialSentinel, live_account: str, admin_account: str
) -> Iterator[Server]:
    """(b): the account exists, its database does not."""
    server = _server(
        credential_sentinel, user=live_account, seeded=False, admin_user=admin_account
    )
    try:
        yield server
    finally:
        server.site.teardown()


@pytest.fixture
def populated(
    credential_sentinel: CredentialSentinel, live_account: str, admin_account: str
) -> Iterator[Server]:
    """(c), (d): a live WordPress database and its account."""
    server = _server(
        credential_sentinel, user=live_account, seeded=True, admin_user=admin_account
    )
    try:
        yield server
    finally:
        server.site.teardown()


def test_a_fresh_server_reports_database_and_user_created(
    fresh: Server, credential_sentinel: CredentialSentinel
) -> None:
    before = fresh.survey(fresh.site.database)
    assert (before.database_exists, before.user_exists) == (False, False)
    spec = fresh.spec(build_archive(fresh.site.work, config=None))
    report = restore(spec).database

    assert report is not None
    assert report.live_database_after == staging_name(fresh.site.database, spec.run_id)
    ensure = report.ensure
    assert (ensure.created_database, ensure.created_user) == (True, True)
    assert (ensure.password_set, ensure.granted) == (True, True)
    assert (report.performed, report.restored) == (True, True)
    assert report.previous_database == "none"
    assert report.loaded_bytes > 0
    after = fresh.survey(report.live_database_after)
    assert (after.database_exists, after.is_wordpress, after.user_exists) == (
        True,
        True,
        True,
    )
    credential_sentinel.add_repr("report", report)
    credential_sentinel.assert_clean()


def test_an_existing_account_without_its_database_is_reported_as_existing(
    account_only: Server,
) -> None:
    before = account_only.survey(account_only.site.database)
    assert (before.database_exists, before.user_exists) == (False, True)
    report = restore(
        account_only.spec(build_archive(account_only.site.work, config=None))
    ).database

    assert report is not None
    assert report.ensure.created_database is True
    assert report.ensure.created_user is False
    assert report.restored is True
    assert report.previous_database == "none"


def test_a_populated_server_with_force_loads_into_staging_and_keeps_the_live(
    populated: Server,
) -> None:
    site = populated.site
    before = populated.survey(site.database)
    assert before.database_exists is True
    assert before.table_count > 0
    checksums = site.checksums()
    archive = build_archive(site.work, config=None)
    with pytest.raises(NonEmptyTargetError) as refused:
        restore(populated.spec(archive))
    assert site.database in refused.value.found
    assert exit_code_for(refused.value) is ExitCode.REFUSED
    force = OverrideSet(waived=frozenset({Override.NONEMPTY_DATABASE}))
    spec = populated.spec(archive, overrides=force)
    report = restore(spec).database

    assert report is not None
    assert report.live_database_before == site.database
    assert report.live_database_after == staging_name(site.database, spec.run_id)
    assert report.ensure.created_database is True
    assert report.previous_database == "retained"
    assert report.restored is True
    assert read_keys(site.config)["DB_NAME"] == report.live_database_after
    assert site.checksums() == checksums


def test_a_truncated_dump_exits_non_zero_and_leaves_the_live_database(
    populated: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = populated.site
    checksums = site.checksums()
    whole = live_restore.WP_DUMP
    monkeypatch.setattr(live_restore, "WP_DUMP", whole[: len(whole) // 2])
    spec = populated.spec(build_archive(site.work, config=None))

    with pytest.raises(BackupError) as caught:
        restore(spec)
    assert exit_code_for(caught.value) is not ExitCode.OK
    assert read_keys(site.config)["DB_NAME"] == site.database
    assert site.checksums() == checksums
    assert site.databases() == [site.database]


def test_the_restored_account_cannot_reach_a_pattern_neighbour(fresh: Server) -> None:
    """AD-28: database escaped the grant's ``_``, so ``wpr<X>...`` is not granted."""
    spec = fresh.spec(build_archive(fresh.site.work, config=None))
    report = restore(spec).database
    assert report is not None
    own = report.live_database_after
    assert "_" in own
    neighbour = own.replace("_", "X", 1)
    with root_connection().cursor() as cursor:
        cursor.execute(f"CREATE DATABASE `{neighbour}`")
    try:
        account = pymysql.connect(
            host=host(),
            port=port(),
            user=fresh.site.user,
            password=fresh.site.password,
            autocommit=True,
        )
        with account.cursor() as cursor:
            cursor.execute("SHOW DATABASES")
            visible = {row[0] for row in cursor.fetchall()}
        account.close()
        assert own in visible
        assert neighbour not in visible
        with pytest.raises(pymysql.err.OperationalError):
            pymysql.connect(
                host=host(),
                port=port(),
                user=fresh.site.user,
                password=fresh.site.password,
                database=neighbour,
            )
    finally:
        with root_connection().cursor() as cursor:
            cursor.execute(f"DROP DATABASE IF EXISTS `{neighbour}`")
