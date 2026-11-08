"""BL-E003-053 — a live database holding only stored code is not empty (FR-7).

Over the CI database: a uniquely named throwaway database with no table or
view, holding one stored procedure (or one event), is surveyed by the
administrator, and the guard refuses a database restore over it. The same
database with nothing in it passes. Only the database this module creates is
dropped.
"""

from __future__ import annotations

import dataclasses
import os
import uuid
from collections.abc import Iterator

import pytest

pytest.importorskip("l3io.wp.database")
pytest.importorskip("pymysql")

from l3io.wp.backup.adapters.database import WpDatabase, admin_source
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.core.restore import NonEmptyTargetError, check_target
from l3io.wp.backup.domain.credentials import ExplicitCredentialSpec
from l3io.wp.backup.domain.database import DatabaseLocation
from l3io.wp.backup.domain.restore import OverrideSet, RestoreMode
from l3io.wp.backup.ports.database import AdminTls
from tests.conftest import CredentialSentinel
from tests.integration.live_cli import admin_account  # noqa: F401 - a fixture
from tests.integration.live_restore import host, port, root_connection
from tests.restore_builders import EMPTY

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        "DB_HOST" not in os.environ, reason="needs the CI database (DB_HOST unset)"
    ),
]

_STORED_CODE = {
    "procedure": "CREATE PROCEDURE `{db}`.`wpk_noop`() BEGIN SELECT 1; END",
    "event": (
        "CREATE EVENT `{db}`.`wpk_tick` ON SCHEDULE EVERY 1 DAY DISABLE DO SELECT 1"
    ),
}


@pytest.fixture
def throwaway_database() -> Iterator[str]:
    name = f"wpk_{uuid.uuid4().hex[:10]}"
    with root_connection().cursor() as cursor:
        cursor.execute(f"CREATE DATABASE `{name}`")
    try:
        yield name
    finally:
        with root_connection().cursor() as cursor:
            cursor.execute(f"DROP DATABASE IF EXISTS `{name}`")


def _survey(account: str, sentinel: CredentialSentinel, name: str) -> object:
    from l3io.wp import database as wpdb

    secrets = SecretRegistry()
    admin = admin_source(
        ExplicitCredentialSpec(account, sentinel.admin_password),
        environ={},
        secrets=secrets,
    )
    database = WpDatabase(
        wpdb.Explicit(account, sentinel.admin_password),
        secrets=secrets,
        admin=admin,
        admin_tls=AdminTls(mode=None, ca_file=None),
    )
    location = DatabaseLocation(host=host(), port=port(), name=name)
    return database.survey_target(location, table_prefix="wp_", wordpress_user=None)


def test_an_empty_throwaway_database_passes_the_guard(
    throwaway_database: str,
    credential_sentinel: CredentialSentinel,
    admin_account: str,  # noqa: F811
) -> None:
    """The control: without stored code the same database is empty."""
    state = _survey(admin_account, credential_sentinel, throwaway_database)
    target = dataclasses.replace(EMPTY, database=state, load_target=None)
    check_target(RestoreMode.DATABASE, OverrideSet.none(), target)


@pytest.mark.parametrize("kind", sorted(_STORED_CODE))
def test_a_database_holding_only_stored_code_is_refused(
    kind: str,
    throwaway_database: str,
    credential_sentinel: CredentialSentinel,
    admin_account: str,  # noqa: F811
) -> None:
    with root_connection().cursor() as cursor:
        cursor.execute(_STORED_CODE[kind].format(db=throwaway_database))
    state = _survey(admin_account, credential_sentinel, throwaway_database)
    assert state.table_count == 0  # type: ignore[attr-defined]
    target = dataclasses.replace(EMPTY, database=state, load_target=None)
    with pytest.raises(NonEmptyTargetError) as caught:
        check_target(RestoreMode.DATABASE, OverrideSet.none(), target)
    assert caught.value.parts == ("database",)
    expected = "1 routine" if kind == "procedure" else "1 event"
    assert expected in caught.value.detail
    credential_sentinel.add_exception(kind, caught.value)
    credential_sentinel.assert_clean()
