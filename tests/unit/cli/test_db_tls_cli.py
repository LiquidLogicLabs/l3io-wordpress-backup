"""Owner decision 56 through the CLI: ``--db-tls`` reaches both roots' database.

No TLS is required unless asked for; ``--db-tls``/``--db-tls-ca`` (or their
variables) are bound to the backup's and the restore's ``WpDatabase``; the
deprecated ``--admin-tls*`` spellings are taken as the new ones, with a
WARNING, and refused when they disagree.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("l3io.wp.database")

from l3io.wp.backup.cli import adapter_sets, main
from l3io.wp.backup.cli import restore as restore_mod
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.ports.database import DbTls
from tests.conftest import pipeline_events
from tests.unit.cli.conftest import FakeDatabase


@pytest.fixture
def bound(monkeypatch: pytest.MonkeyPatch) -> list[DbTls | None]:
    """Every ``db_tls`` a ``WpDatabase`` was built with."""
    seen: list[DbTls | None] = []
    database = FakeDatabase()

    def build(credentials: object, *, secrets: object, admin: object = None,
              db_tls: DbTls | None = None) -> FakeDatabase:  # fmt: skip
        seen.append(db_tls)
        return database

    monkeypatch.setattr(adapter_sets, "WpDatabase", build)
    return seen


def _backup(site: Path, tmp_path: Path, *extra: str) -> int:
    archive = tmp_path / "out.tar.gz"
    return main(["backup", "--wp-dir", str(site), "--archive", str(archive), *extra])


def test_a_backup_requires_no_tls_by_default(
    site: Path, tmp_path: Path, bound: list[DbTls | None]
) -> None:
    assert _backup(site, tmp_path) == 0
    assert bound
    assert all(tls == DbTls() for tls in bound)


@pytest.mark.parametrize("spelling", ["flags", "variables"])
def test_a_backup_binds_db_tls(
    site: Path,
    tmp_path: Path,
    bound: list[DbTls | None],
    monkeypatch: pytest.MonkeyPatch,
    spelling: str,
) -> None:
    ca = tmp_path / "ca.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
    extra: list[str] = []
    if spelling == "flags":
        extra = ["--db-tls", "verify-ca", "--db-tls-ca", str(ca)]
    else:
        monkeypatch.setenv("L3IO_WP_BACKUP_DB_TLS", "verify-ca")
        monkeypatch.setenv("L3IO_WP_BACKUP_DB_TLS_CA", str(ca))
    assert _backup(site, tmp_path, *extra) == 0
    assert bound
    assert all(tls == DbTls(mode="verify-ca", ca_file=ca) for tls in bound)


def test_a_ca_file_without_a_verifying_mode_is_usage(
    site: Path, tmp_path: Path, bound: list[DbTls | None]
) -> None:
    assert _backup(site, tmp_path, "--db-tls-ca", str(tmp_path / "ca.pem")) == int(
        ExitCode.USAGE
    )
    assert bound == []


def _restore_tls(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *extra: str) -> DbTls:
    """The ``db_tls`` ``run_restore`` puts in its request."""
    seen: list[DbTls] = []

    class _Stop(Exception):
        pass

    def drive(request: restore_mod.RestoreRequest, **_: object) -> object:
        seen.append(request.db_tls)
        raise _Stop

    monkeypatch.setattr(restore_mod, "drive_restore", drive)
    site = tmp_path / "www"
    site.mkdir(exist_ok=True)
    archive = tmp_path / "a.tar.gz"
    archive.write_bytes(b"")
    argv = ["restore", "--wp-dir", str(site), "--archive", str(archive), *extra]
    main(argv)
    (tls,) = seen
    return tls


def test_the_deprecated_spellings_are_taken_as_the_new_ones(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ca = tmp_path / "ca.pem"
    tls = _restore_tls(
        monkeypatch,
        tmp_path,
        "--admin-tls",
        "verify-identity",
        "--admin-tls-ca",
        str(ca),
    )
    assert tls == DbTls(mode="verify-identity", ca_file=ca)
    deprecated = [
        e for e in pipeline_events(caplog) if e["event"] == "deprecated_option"
    ]
    assert {e["option"] for e in deprecated} == {"--admin-tls", "--admin-tls-ca"}
    assert {e["replacement"] for e in deprecated} == {"--db-tls", "--db-tls-ca"}


def test_disagreeing_spellings_are_usage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    site = tmp_path / "www"
    site.mkdir()
    archive = tmp_path / "a.tar.gz"
    archive.write_bytes(b"")
    argv = ["restore", "--wp-dir", str(site), "--archive", str(archive)]
    code = main([*argv, "--db-tls", "required", "--admin-tls", "verify-ca"])
    assert code == int(ExitCode.USAGE)
    assert "--admin-tls and --db-tls disagree" in capsys.readouterr().err


def test_a_restore_requires_no_tls_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert _restore_tls(monkeypatch, tmp_path) == DbTls()
