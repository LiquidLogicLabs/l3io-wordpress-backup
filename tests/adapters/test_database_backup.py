"""ADR-0015 — ``WpDatabase``, the backup half of the database adapter.

The ``integration`` tests need a live server: the CI ``db`` service (MySQL 8.0
/ 8.4, MariaDB 11.4), reached through the job's ``DB_HOST`` and
``DB_PASSWORD`` (root account, database ``wptest``). They skip when
``DB_HOST`` is unset. Credentials reach the adapter only through a
``wp-config.php`` written into ``tmp_path`` — no other credential is given.

The remaining tests replace database's functions with doubles, to pin how each
failure is re-raised (AD-14).
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

wpdb = pytest.importorskip("l3io.wp.database")

from l3io.wp.backup.adapters.database import (  # noqa: E402
    WpDatabase,
    require_wp_config_credentials,
    site_descriptor_from_wp_config,
)
from l3io.wp.backup.adapters.redaction import SecretRegistry  # noqa: E402
from l3io.wp.backup.domain.database import (  # noqa: E402
    DatabaseLocation,
    DumpResult,
    Readiness,
)
from l3io.wp.backup.domain.errors import (  # noqa: E402
    BackupError,
    CredentialsError,
    DatabaseRefusedError,
    DatabaseUnreachableError,
    DumpFailedError,
    MissingExtraError,
)

LOCATION = DatabaseLocation(host="db.invalid", port=3306, name="wptest")

live = pytest.mark.skipif(
    "DB_HOST" not in os.environ, reason="needs the CI database (DB_HOST unset)"
)


def _site(tmp_path: Path, *, password: str) -> tuple[Path, Path]:
    site = tmp_path / "site"
    site.mkdir()
    config = site / "wp-config.php"
    config.write_text(
        "<?php\n"
        f"define('DB_NAME', {os.environ.get('DB_NAME', 'wptest')!r});\n"
        f"define('DB_USER', {os.environ.get('DB_USER', 'root')!r});\n"
        f"define('DB_PASSWORD', {password!r});\n"
        f"define('DB_HOST', {os.environ['DB_HOST']!r});\n",
        encoding="utf-8",
    )
    return site, config


def _adapter(tmp_path: Path, *, password: str) -> tuple[WpDatabase, DatabaseLocation]:
    site, config = _site(tmp_path, password=password)
    descriptor, credentials = site_descriptor_from_wp_config(site, config_file=config)
    require_wp_config_credentials(credentials)  # wp-config.php is the provider
    return (
        WpDatabase(credentials, secrets=SecretRegistry()),
        descriptor.database_location(),
    )


# --------------------------------------------------------------------------
# Against the CI database
# --------------------------------------------------------------------------


@pytest.mark.integration
@live
def test_check_dump_is_ready(tmp_path: Path) -> None:
    database, location = _adapter(tmp_path, password=os.environ["DB_PASSWORD"])
    readiness = database.check_dump(location)
    assert readiness.ready, readiness.blockers
    assert readiness.blockers == ()


@pytest.mark.integration
@live
def test_dump_to_writes_an_owner_only_dump(tmp_path: Path) -> None:
    database, location = _adapter(tmp_path, password=os.environ["DB_PASSWORD"])
    destination = tmp_path / "work" / "dump.sql"
    destination.parent.mkdir(mode=0o700)
    result = database.dump_to(location, destination)
    assert isinstance(result, DumpResult)
    assert result.bytes == destination.stat().st_size > 0
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


@pytest.mark.integration
@live
def test_a_wrong_password_is_a_credentials_error(tmp_path: Path) -> None:
    database, location = _adapter(tmp_path, password="definitely-not-it")
    destination = tmp_path / "dump.sql"
    with pytest.raises(CredentialsError) as caught:
        database.dump_to(location, destination)
    assert "definitely-not-it" not in str(caught.value)
    assert not destination.exists()
    readiness = database.check_dump(location)
    assert readiness == Readiness(
        ready=False, blockers=("authentication_failed",), warnings=()
    )


# --------------------------------------------------------------------------
# Against doubles
# --------------------------------------------------------------------------


def _verdict(*codes: str) -> object:
    findings = tuple(wpdb.Finding(wpdb.Code(c), c) for c in codes)
    return wpdb.Verdict("dump", findings)


@pytest.fixture
def database() -> WpDatabase:
    return WpDatabase(wpdb.Explicit("someone", "never-shown"), secrets=SecretRegistry())


def test_a_transfer_error_is_a_dump_failure(
    database: WpDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_: object, **__: object) -> object:
        msg = "mysqldump was killed"
        raise wpdb.TransferError(msg, _verdict(), -9, "")

    monkeypatch.setattr(wpdb, "dump", broken)
    with pytest.raises(DumpFailedError) as caught:
        database.dump_to(LOCATION, tmp_path / "dump.sql")
    # No chain (BL-E002-053): the TransferError's text is the tool's stderr.
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__
    assert caught.value.exit_status == -9


@pytest.mark.parametrize(
    ("codes", "expected"),
    [
        (("unreachable",), DatabaseUnreachableError),
        (("authentication_failed",), CredentialsError),
        (("credentials_unobtainable",), CredentialsError),
        (("client_tool_missing",), DatabaseRefusedError),
    ],
)
def test_a_refusal_is_re_raised_by_its_cause(
    database: WpDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    codes: tuple[str, ...],
    expected: type[Exception],
) -> None:
    def refused(*_: object, **__: object) -> object:
        raise wpdb.RefusedError(_verdict(*codes))

    monkeypatch.setattr(wpdb, "dump", refused)
    with pytest.raises(expected) as caught:
        database.dump_to(LOCATION, tmp_path / "dump.sql")
    assert "never-shown" not in str(caught.value)
    if isinstance(caught.value, DatabaseRefusedError):
        assert caught.value.findings == codes


def test_the_dump_passes_no_key_and_returns_the_size(
    database: WpDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def fake(
        connection: object, credentials: object, to: object, **kw: object
    ) -> object:
        seen.update(connection=connection, to=to, kw=kw)
        return wpdb.TransferResult(
            bytes=12, duration_ms=3.5, tool="t", server=None, verdict=_verdict()
        )

    monkeypatch.setattr(wpdb, "dump", fake)
    result = database.dump_to(LOCATION, tmp_path / "dump.sql")
    assert result == DumpResult(bytes=12, duration_ms=3.5)
    assert seen["kw"] == {}
    assert seen["connection"] == wpdb.Connection(
        host="db.invalid", database="wptest", port=3306
    )


def test_check_dump_copies_codes_and_does_not_raise(
    database: WpDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        wpdb,
        "check_dump",
        lambda *_: _verdict("client_tool_missing", "stored_objects_present"),
    )
    assert database.check_dump(LOCATION) == Readiness(
        ready=False,
        blockers=("client_tool_missing",),
        warnings=("stored_objects_present",),
    )


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (wpdb.AccessDeniedError("never-shown"), DatabaseRefusedError),
        (wpdb.MissingDependencyError("x", "never-shown"), MissingExtraError),
        (wpdb.DatabaseError("never-shown"), BackupError),
    ],
    ids=lambda v: type(v).__name__ if isinstance(v, Exception) else v.__name__,
)
def test_check_dump_wraps_every_database_error(
    database: WpDatabase,
    monkeypatch: pytest.MonkeyPatch,
    raised: Exception,
    expected: type[Exception],
) -> None:
    def broken(*_: object) -> object:
        raise raised

    monkeypatch.setattr(wpdb, "check_dump", broken)
    with pytest.raises(BackupError) as caught:
        database.check_dump(LOCATION)
    assert isinstance(caught.value, expected)
    assert caught.value.__cause__ is raised
    assert "never-shown" not in str(caught.value)
    if isinstance(caught.value, DatabaseRefusedError):
        assert caught.value.findings == ("missing_privilege",)


def test_the_missing_extra_is_reported_before_any_write(
    database: WpDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "l3io.wp.database", None)
    destination = tmp_path / "dump.sql"
    with pytest.raises(MissingExtraError):
        database.dump_to(LOCATION, destination)
    with pytest.raises(MissingExtraError):
        database.check_dump(LOCATION)
    assert not destination.exists()


def test_repr_names_the_source_not_the_secret(database: WpDatabase) -> None:
    assert "never-shown" not in repr(database)
