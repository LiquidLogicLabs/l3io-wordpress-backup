"""Close-out owner decisions 2026-11-07, items 1-3: the three safety refusals.

1. BL-E002-098 — a restore running as root is refused (``USAGE``) before the
   Run opens unless ``--allow-root``; with it, a WARNING names the race.
2. BL-E002-063 — a backup that dumps refuses a non-local ``DB_HOST`` before any
   connection unless ``--confirm-db-host`` names it.
3. BL-E002-064 — a backup whose target resolves inside the Site Root is refused
   (``USAGE``) before any effect unless ``--allow-target-in-site-root``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from structlog.testing import capture_logs

pytest.importorskip("l3io.wp.database")

from l3io.wp.backup.adapters.database import database_host_is_local
from l3io.wp.backup.cli import main
from l3io.wp.backup.cli import restore as restore_mod
from l3io.wp.backup.domain.errors import (
    DatabaseHostUnconfirmedError,
    RootRestoreRefusedError,
    TargetInsideSiteRootError,
)
from l3io.wp.backup.domain.exit_codes import ExitCode
from tests.restore_root_rig import Rig
from tests.unit.cli.conftest import FakeDatabase, RecordingSiteLock

# --------------------------------------------------------------------------
# 1. root restore
# --------------------------------------------------------------------------


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    return Rig(tmp_path, monkeypatch)


def test_a_root_restore_is_refused_before_the_lock(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(restore_mod, "_running_as_root", lambda: True)
    lock = RecordingSiteLock()
    with pytest.raises(RootRestoreRefusedError) as caught:
        restore_mod.drive_restore(
            rig.request(allow_root=False), ctx=None, site_lock=lock
        )
    assert lock.events == []
    assert caught.value.exit_code is ExitCode.USAGE
    assert caught.value.arguments == ("allow_root",)
    assert "--allow-root" in str(caught.value)


def test_allow_root_proceeds_and_warns_of_the_race(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(restore_mod, "_running_as_root", lambda: True)
    with capture_logs() as logs:
        rig.drive(allow_root=True)
    (warning,) = [e for e in logs if e["event"] == "restore_running_as_root"]
    assert warning["log_level"] == "warning"
    assert "symlink" in warning["risk"]


def test_a_non_root_restore_neither_refuses_nor_warns(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(restore_mod, "_running_as_root", lambda: False)
    with capture_logs() as logs:
        rig.drive()
    assert all(e["event"] != "restore_running_as_root" for e in logs)


# --------------------------------------------------------------------------
# 2. non-local DB_HOST
# --------------------------------------------------------------------------


def _set_host(site: Path, host: str) -> None:
    config = site / "wp-config.php"
    text = config.read_text(encoding="utf-8")
    config.write_text(
        text.replace(
            "define('DB_HOST', 'localhost');", f"define('DB_HOST', '{host}');"
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("host", "local"),
    [
        ("localhost", True),
        ("LOCALHOST", True),
        ("127.0.0.1", True),
        ("/run/mysqld/mysqld.sock", True),
        ("db.example", False),
        ("10.0.0.5", False),
        ("localhost.example", False),
    ],
)
def test_which_hosts_are_local(host: str, local: bool) -> None:
    assert database_host_is_local(host) is local


@pytest.mark.parametrize("mode", ["db", "all"])
def test_a_remote_db_host_is_refused_before_any_connection(
    site: Path,
    tmp_path: Path,
    fake_database: FakeDatabase,
    capsys: pytest.CaptureFixture[str],
    mode: str,
) -> None:
    _set_host(site, "db.example")
    final = tmp_path / "out.tar.gz"
    code = main(
        ["backup", "--wp-dir", str(site), "--archive", str(final), "--mode", mode]
    )
    assert code == int(DatabaseHostUnconfirmedError.exit_code)
    assert fake_database.checks == []
    assert fake_database.calls == []
    assert not final.exists()
    assert "--confirm-db-host db.example" in capsys.readouterr().err


def test_confirming_the_host_lets_the_backup_run(
    site: Path, tmp_path: Path, fake_database: FakeDatabase
) -> None:
    _set_host(site, "db.example")
    final = tmp_path / "out.tar.gz"
    argv = ["backup", "--wp-dir", str(site), "--archive", str(final)]
    assert main([*argv, "--confirm-db-host", "DB.Example"]) == 0
    assert len(fake_database.calls) == 1


def test_confirming_another_host_is_refused(
    site: Path, tmp_path: Path, fake_database: FakeDatabase
) -> None:
    _set_host(site, "db.example")
    final = tmp_path / "out.tar.gz"
    argv = ["backup", "--wp-dir", str(site), "--archive", str(final)]
    code = main([*argv, "--confirm-db-host", "other.example"])
    assert code == int(ExitCode.REFUSED)
    assert fake_database.calls == []


def test_a_files_backup_does_not_ask_for_the_host(
    site: Path, tmp_path: Path, fake_database: FakeDatabase
) -> None:
    _set_host(site, "db.example")
    final = tmp_path / "out.tar.gz"
    argv = ["backup", "--wp-dir", str(site), "--archive", str(final)]
    assert main([*argv, "--mode", "files"]) == 0


# --------------------------------------------------------------------------
# 3. target inside the Site Root
# --------------------------------------------------------------------------


@pytest.mark.parametrize("inside", ["out.tar.gz", "sub/dir/out.tar.gz"])
def test_a_target_inside_the_site_root_is_refused(
    site: Path,
    fake_database: FakeDatabase,
    capsys: pytest.CaptureFixture[str],
    inside: str,
) -> None:
    (site / "sub" / "dir").mkdir(parents=True)
    final = site / inside
    code = main(["backup", "--wp-dir", str(site), "--archive", str(final)])
    assert code == int(ExitCode.USAGE)
    assert fake_database.calls == []
    assert not final.exists()
    err = capsys.readouterr().err
    assert "--allow-target-in-site-root" in err
    assert "outside the Site Root" in err


def test_the_default_target_from_the_docroot_is_refused(
    site: Path,
    fake_database: FakeDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(site)
    assert main(["backup", "--wp-dir", str(site)]) == int(
        TargetInsideSiteRootError.exit_code
    )
    assert fake_database.calls == []


def test_a_symlink_into_the_site_root_is_refused(
    site: Path, tmp_path: Path, fake_database: FakeDatabase
) -> None:
    link = tmp_path / "into-site"
    link.symlink_to(site)
    code = main(["backup", "--wp-dir", str(site), "--archive", str(link / "a.tar.gz")])
    assert code == int(ExitCode.USAGE)


def test_allowing_it_writes_and_warns(
    site: Path, fake_database: FakeDatabase, capsys: pytest.CaptureFixture[str]
) -> None:
    final = site / "out.tar.gz"
    argv = ["backup", "--wp-dir", str(site), "--archive", str(final)]
    assert main([*argv, "--allow-target-in-site-root"]) == 0
    assert final.is_file()
    assert "archive_target_in_site_root" in capsys.readouterr().err
