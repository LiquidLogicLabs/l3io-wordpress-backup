"""Owner decision 56 (amending BL-E002-114) — opt-in TLS against the CI database.

Four live cases over the CI server:

* no ``--db-tls`` → no TLS is required, so the survey succeeds whatever the
  host (as WordPress connects);
* TLS switched off on the server (``tls_disabled``, below) with
  ``required`` → the handshake cannot complete → ``DatabaseTlsHandshakeError``,
  exit ``UNREACHABLE``;
* ``verify-identity`` against the server's auto-generated self-signed
  certificate → ``DatabaseTlsVerificationError``, exit ``REFUSED``;
* ``required`` against the server as configured (TLS on) → the survey
  succeeds.

``tls_disabled`` turns TLS off at run time (MySQL >= 8.0.21,
``ALTER INSTANCE RELOAD TLS``) and always restores the original
certificate settings in teardown; a failed restore fails loudly.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator

import pytest

pytest.importorskip("l3io.wp.database")
pytest.importorskip("pymysql")

from l3io.wp.backup.adapters.database import (
    DatabaseTlsHandshakeError,
    DatabaseTlsVerificationError,
    WpDatabase,
    admin_source,
)
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.domain.credentials import ExplicitCredentialSpec
from l3io.wp.backup.domain.database import DatabaseLocation
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.ports.database import DbTls
from tests.conftest import CredentialSentinel, skip_unsupported_engine
from tests.integration.live_cli import admin_account  # noqa: F401 - a fixture
from tests.integration.live_restore import host, port, root_connection

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        "DB_HOST" not in os.environ, reason="needs the CI database (DB_HOST unset)"
    ),
]

_TLS_FILES = ("ssl_cert", "ssl_key", "ssl_ca")
_MISSING = "/nonexistent-l3io-tls-test.pem"


def _tls_enabled(cursor: object) -> str:
    """The main channel's ``Enabled`` value (``Yes``/``No``).

    ``performance_schema.tls_channel_status`` (MySQL >= 8.0.21) rather than
    ``have_ssl``, which MySQL 8.4 removed.
    """
    cursor.execute(  # type: ignore[attr-defined]
        "SELECT VALUE FROM performance_schema.tls_channel_status "
        "WHERE CHANNEL = 'mysql_main' AND PROPERTY = 'Enabled'"
    )
    row = cursor.fetchone()  # type: ignore[attr-defined]
    return "" if row is None else str(row[0]).upper()


def _runtime_tls_reload_supported(cursor: object) -> str | None:
    """``None`` when the server is MySQL >= 8.0.21, else the skip reason."""
    cursor.execute("SELECT VERSION(), @@version_comment")  # type: ignore[attr-defined]
    version, comment = cursor.fetchone()  # type: ignore[attr-defined]
    text = f"{version} {comment}"
    if "mariadb" in text.lower():
        return f"server is MariaDB ({version}); ALTER INSTANCE RELOAD TLS is MySQL-only"
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", str(version))
    if match is None or tuple(int(p) for p in match.groups()) < (8, 0, 21):
        return (
            f"server {version} predates MySQL 8.0.21 (no runtime TLS reload "
            "with performance_schema.tls_channel_status)"
        )
    return None


@pytest.fixture
def tls_disabled() -> Iterator[None]:
    """TLS off on the CI server for one test; the originals always restored.

    One root connection is held across the test, so teardown does not depend
    on a new connection authenticating while TLS is off. The restore runs
    whenever a global was changed, including when the switch-off check fails.
    """
    connection = root_connection()
    try:
        with connection.cursor() as cursor:
            reason = _runtime_tls_reload_supported(cursor)
            if reason is not None:
                skip_unsupported_engine(reason)
            cursor.execute("SELECT @@ssl_cert, @@ssl_key, @@ssl_ca")
            originals = dict(zip(_TLS_FILES, cursor.fetchone(), strict=True))
            before = _tls_enabled(cursor)
        try:
            with connection.cursor() as cursor:
                for name in _TLS_FILES:
                    cursor.execute(f"SET GLOBAL {name} = %s", (_MISSING,))
                cursor.execute("ALTER INSTANCE RELOAD TLS NO ROLLBACK ON ERROR")
                assert _tls_enabled(cursor) == "NO", "TLS did not switch off"
            yield
        finally:
            with connection.cursor() as cursor:
                for name in _TLS_FILES:
                    cursor.execute(f"SET GLOBAL {name} = %s", (originals[name],))
                cursor.execute("ALTER INSTANCE RELOAD TLS")
                after = _tls_enabled(cursor)
            if after != before:
                pytest.fail(
                    f"TLS NOT restored on the CI server: Enabled={after!r}, "
                    f"was {before!r}; originals {originals!r}"
                )
    finally:
        connection.close()


def _database(
    account: str, sentinel: CredentialSentinel, tls: DbTls | None
) -> tuple[WpDatabase, DatabaseLocation]:
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
        db_tls=tls,
    )
    return database, DatabaseLocation(host=host(), port=port(), name="mysql")


# MySQL-only: MariaDB has no ``ALTER INSTANCE RELOAD TLS``, so on the mariadb
# legs ``tls_disabled`` skips this with EngineUnsupported -- by design, and the
# only skip tests/conftest.py lets an integration test take in CI.
@pytest.mark.mysql_only
def test_a_server_refusing_tls_is_unreachable(
    tls_disabled: None,
    credential_sentinel: CredentialSentinel,
    admin_account: str,  # noqa: F811
) -> None:
    tls = DbTls(mode="required")
    database, location = _database(admin_account, credential_sentinel, tls)
    with pytest.raises(DatabaseTlsHandshakeError) as caught:
        database.survey_target(location, table_prefix="wp_", wordpress_user=None)
    assert caught.value.exit_code is ExitCode.UNREACHABLE
    credential_sentinel.add_exception("tls refusal", caught.value)
    credential_sentinel.assert_clean()


def test_an_unverifiable_certificate_is_refused(
    credential_sentinel: CredentialSentinel,
    admin_account: str,  # noqa: F811
) -> None:
    """The CI server's auto-generated certificate is self-signed: no system CA."""
    database, location = _database(
        admin_account,
        credential_sentinel,
        DbTls(mode="verify-identity", ca_file=None),
    )
    with pytest.raises(DatabaseTlsVerificationError) as caught:
        database.survey_target(location, table_prefix="wp_", wordpress_user=None)
    assert caught.value.exit_code is ExitCode.REFUSED
    credential_sentinel.add_exception("tls verification", caught.value)
    credential_sentinel.assert_clean()


@pytest.mark.parametrize("tls", [DbTls(), None], ids=["no-mode", "unbound"])
def test_no_tls_is_required_by_default(
    credential_sentinel: CredentialSentinel,
    admin_account: str,  # noqa: F811
    tls: DbTls | None,
) -> None:
    database, location = _database(admin_account, credential_sentinel, tls)
    survey = database.survey_target(location, table_prefix="wp_", wordpress_user=None)
    assert survey is not None
    credential_sentinel.assert_clean()


def test_required_tls_against_a_tls_server_surveys(
    credential_sentinel: CredentialSentinel,
    admin_account: str,  # noqa: F811
) -> None:
    database, location = _database(
        admin_account, credential_sentinel, DbTls(mode="required", ca_file=None)
    )
    survey = database.survey_target(location, table_prefix="wp_", wordpress_user=None)
    assert survey is not None
    credential_sentinel.assert_clean()
