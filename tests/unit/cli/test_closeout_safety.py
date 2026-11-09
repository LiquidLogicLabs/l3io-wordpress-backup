"""Owner decisions 54, 58, 60 and 61 (2026-11-08): what replaced three refusals.

Each reverses a 2026-11-07 refusal for unattended use:

1. Decision 58 (reversing decision 1, BL-E002-098): a restore as root runs as
   the Site Root's owner (``cli/privileges.py``, its own tests); the library,
   which never switches its host's user, refuses a restore running as root.
   ``--allow-root`` is gone.
2. Decision 54 (reversing decision 2, BL-E002-063): a backup's database host
   is logged and shown in the Dry Run, never confirmed. ``--confirm-db-host``
   is gone.
3. Decision 60 (reversing decision 3, BL-E002-064): the default Archive is
   never inside the Site Root (the current directory when it is outside the
   site, else the per-user data directory, or ``--archive-dir``); a target the
   operator puts there is written with a WARNING.
   ``--allow-target-in-site-root`` is gone.
4. Decision 61: a database password below the redaction floor is accepted,
   with WARNING ``credential_below_redaction_floor`` naming its source.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

pytest.importorskip("l3io.wp.database")

from l3io.wp.backup.adapters.database import database_host_is_local
from l3io.wp.backup.cli import main, privileges
from l3io.wp.backup.cli import restore as restore_mod
from l3io.wp.backup.domain.errors import RootRestoreRefusedError
from l3io.wp.backup.domain.exit_codes import ExitCode
from tests.conftest import pipeline_events
from tests.restore_root_rig import Rig
from tests.unit.cli.conftest import FakeDatabase, RecordingSiteLock

# --------------------------------------------------------------------------
# 1. root restore (decision 58)
# --------------------------------------------------------------------------


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    return Rig(tmp_path, monkeypatch)


def test_a_library_restore_as_root_is_refused_before_the_lock(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(privileges, "running_as_root", lambda: True)
    lock = RecordingSiteLock()
    with pytest.raises(RootRestoreRefusedError) as caught:
        restore_mod.drive_restore(rig.request(), ctx=None, site_lock=lock)
    assert lock.events == []
    assert caught.value.exit_code is ExitCode.USAGE
    assert caught.value.arguments == ("site_root",)
    assert caught.value.reason == privileges.LIBRARY_RUNS_AS_HOST


def test_a_non_root_restore_neither_refuses_nor_switches(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(privileges, "running_as_root", lambda: False)
    with capture_logs() as logs:
        rig.drive()
    assert all(e["event"] != "restore_runs_as_site_owner" for e in logs)


@pytest.mark.parametrize(
    "removed", ["--allow-root", "--confirm-db-host", "--allow-target-in-site-root"]
)
def test_the_removed_flags_are_unrecognised(
    removed: str, capsys: pytest.CaptureFixture[str]
) -> None:
    command = "restore" if removed == "--allow-root" else "backup"
    argv = [command, "--wp-dir", "/nonexistent", "--archive", "/nonexistent/a"]
    with pytest.raises(SystemExit) as exited:
        main([*argv, removed, *(["db"] if removed == "--confirm-db-host" else [])])
    assert exited.value.code == int(ExitCode.USAGE)
    assert removed in capsys.readouterr().err


# --------------------------------------------------------------------------
# 2. the database host (decision 54)
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
def test_a_remote_db_host_is_logged_and_the_backup_runs(
    site: Path,
    tmp_path: Path,
    fake_database: FakeDatabase,
    caplog: pytest.LogCaptureFixture,
    mode: str,
) -> None:
    _set_host(site, "db.example")
    final = tmp_path / "out.tar.gz"
    code = main(
        ["backup", "--wp-dir", str(site), "--archive", str(final), "--mode", mode]
    )
    logs = pipeline_events(caplog)
    assert code == 0
    assert len(fake_database.calls) == 1
    assert final.is_file()
    (host,) = [e for e in logs if e["event"] == "database_host"]
    assert host["level"] == "info"
    assert (host["db_host"], host["db_port"], host["local"]) == (
        "db.example",
        3306,
        False,
    )


def test_the_dry_run_names_the_database_host(
    site: Path,
    tmp_path: Path,
    fake_database: FakeDatabase,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_host(site, "db.example:3307")
    final = tmp_path / "out.tar.gz"
    assert (
        main(["--whatIf", "backup", "--wp-dir", str(site), "--archive", str(final)])
        == 0
    )
    assert "dump wptest from db.example:3307" in capsys.readouterr().out
    assert not final.exists()


def test_a_files_backup_does_not_log_the_host(
    site: Path,
    tmp_path: Path,
    fake_database: FakeDatabase,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _set_host(site, "db.example")
    final = tmp_path / "out.tar.gz"
    argv = ["backup", "--wp-dir", str(site), "--archive", str(final)]
    assert main([*argv, "--mode", "files"]) == 0
    logs = pipeline_events(caplog)
    assert any(e["event"] == "archive_target_chosen" for e in logs)
    assert all(e["event"] != "database_host" for e in logs)


# --------------------------------------------------------------------------
# 3. where the Archive goes (decision 60)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("inside", ["out.tar.gz", "sub/dir/out.tar.gz"])
def test_a_target_inside_the_site_root_is_written_with_a_warning(
    site: Path,
    fake_database: FakeDatabase,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    inside: str,
) -> None:
    (site / "sub" / "dir").mkdir(parents=True)
    final = site / inside
    code = main(["backup", "--wp-dir", str(site), "--archive", str(final)])
    logs = pipeline_events(caplog)
    assert code == 0
    assert final.is_file()
    (warning,) = [e for e in logs if e["event"] == "archive_target_in_site_root"]
    assert warning["level"] == "warning"
    assert "archive_target_in_site_root" in capsys.readouterr().err


def test_a_symlink_into_the_site_root_is_inside_it(
    site: Path,
    tmp_path: Path,
    fake_database: FakeDatabase,
    caplog: pytest.LogCaptureFixture,
) -> None:
    link = tmp_path / "into-site"
    link.symlink_to(site)
    code = main(["backup", "--wp-dir", str(site), "--archive", str(link / "a.tar.gz")])
    logs = pipeline_events(caplog)
    assert code == 0
    assert any(e["event"] == "archive_target_in_site_root" for e in logs)


def _chosen(logs: list[dict[str, Any]]) -> dict[str, Any]:
    (chosen,) = [e for e in logs if e["event"] == "archive_target_chosen"]
    return chosen


def test_the_default_target_from_the_docroot_is_the_data_directory(
    site: Path,
    fake_database: FakeDatabase,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.chdir(site)
    assert main(["backup", "--wp-dir", str(site)]) == 0
    logs = pipeline_events(caplog)
    archives = Path(os.environ["XDG_DATA_HOME"]) / "l3io-wp-backup" / "archives"
    (written,) = archives.glob("wpbackup2-wptest-*.tar.gz")
    assert not list(site.glob("wpbackup2-*"))
    assert stat.S_IMODE(archives.stat().st_mode) == 0o700
    chosen = _chosen(logs)
    assert chosen["chosen_by"] == "the per-user data directory"
    assert chosen["path"] == str(written)
    assert all(e["event"] != "archive_target_in_site_root" for e in logs)


def test_the_default_target_outside_the_site_is_the_current_directory(
    site: Path,
    tmp_path: Path,
    fake_database: FakeDatabase,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    here = tmp_path / "here"
    here.mkdir()
    monkeypatch.chdir(here)
    assert main(["backup", "--wp-dir", str(site)]) == 0
    logs = pipeline_events(caplog)
    (written,) = here.glob("wpbackup2-wptest-*.tar.gz")
    assert _chosen(logs)["path"] == str(written.resolve())


@pytest.mark.parametrize("spelling", ["flag", "variable"])
def test_archive_dir_names_the_directory(
    site: Path,
    tmp_path: Path,
    fake_database: FakeDatabase,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    spelling: str,
) -> None:
    backups = tmp_path / "backups"
    backups.mkdir()
    argv = ["--whatIf", "backup", "--wp-dir", str(site)]
    if spelling == "flag":
        argv += ["--archive-dir", str(backups)]
    else:
        monkeypatch.setenv("L3IO_WP_BACKUP_ARCHIVE_DIR", str(backups))
    assert main(argv) == 0
    logs = pipeline_events(caplog)
    assert f"publish to file:{backups}/wpbackup2-wptest-" in capsys.readouterr().out
    assert _chosen(logs)["chosen_by"] == "--archive-dir"
    assert list(backups.iterdir()) == []


def test_a_backup_refuses_archive_latest(
    site: Path, fake_database: FakeDatabase, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["backup", "--wp-dir", str(site), "--archive", "latest"])
    assert code == int(ExitCode.USAGE)
    assert fake_database.calls == []
    assert "latest" in capsys.readouterr().err


# --------------------------------------------------------------------------
# 4. a short database password (decision 61)
# --------------------------------------------------------------------------


def test_a_short_database_password_is_accepted_with_a_warning(
    site: Path,
    tmp_path: Path,
    fake_database: FakeDatabase,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = tmp_path / "settings.toml"
    config.write_text('db_user = "wp"\ndb_password = "q7z"\n', encoding="utf-8")
    config.chmod(0o600)
    final = tmp_path / "out.tar.gz"
    argv = ["--config", str(config), "backup", "--wp-dir", str(site)]
    assert main([*argv, "--archive", str(final)]) == 0
    logs = pipeline_events(caplog)
    (warning,) = [e for e in logs if e["event"] == "credential_below_redaction_floor"]
    assert warning["level"] == "warning"
    assert warning["source"] == "explicit"
    assert "q7z" not in repr(warning)
    assert final.is_file()
