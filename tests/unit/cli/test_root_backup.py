"""Owner decisions 67 and 68 — a backup started as root runs as the Site Root's owner.

In-process, over :class:`tests.root_mode.Kernel` (a recording ``IdSystem``),
so these hold as root and as anyone: the CLI root refuses a root-owned Site
Root before it reads anything; reads the passphrase and resolves the database
credentials (the whole chain, ``wp-config.php`` included) while still root;
switches; refuses an Archive directory the owner cannot write before the Run
opens, a Dry Run too; and the Run then reads no credential again. The library
(``drive_backup``, ``api.backup``) refuses a Run as root. The real switch, in
a child process, is ``tests/integration/test_root_backup.py``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("l3io.wp.database")

from l3io.wp.backup import api
from l3io.wp.backup.adapters import database as database_mod
from l3io.wp.backup.cli import adapter_sets, main, privileges
from l3io.wp.backup.cli import backup as backup_mod
from l3io.wp.backup.cli.backup import BackupRequest, drive_backup
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.errors import (
    ArchiveDirectoryNotWritableError,
    RootBackupRefusedError,
)
from l3io.wp.backup.domain.exit_codes import ExitCode
from tests.conftest import pipeline_events
from tests.root_mode import Kernel
from tests.unit.cli.conftest import FakeDatabase, RecordingSiteLock

PASSPHRASE = "root-backup-passphrase-0042"


@pytest.fixture
def kernel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Kernel, privileges.SiteOwner]:
    """Root until the switch, then the owner: ``running_as_root`` follows it."""
    ids = Kernel()
    home = tmp_path / "owner-home"
    home.mkdir()
    owner = privileges.SiteOwner(uid=33, gid=33, user="www-data", home=str(home))
    monkeypatch.setattr(privileges, "running_as_root", lambda: ids.uid == 0)
    monkeypatch.setattr(privileges, "site_owner", lambda _root: owner)
    monkeypatch.setattr(privileges, "_OS", ids)
    # The switch sets HOME on os.environ: restored after the test.
    monkeypatch.setenv("HOME", os.environ.get("HOME", str(tmp_path)))
    return ids, owner


def _argv(site: Path, *extra: str) -> list[str]:
    return ["backup", "--wp-dir", str(site), *extra]


def test_a_root_backup_reads_first_then_switches_then_runs(
    site: Path,
    tmp_path: Path,
    fake_database: FakeDatabase,
    kernel: tuple[Kernel, privileges.SiteOwner],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ids, owner = kernel
    order: list[str] = []
    reads_as: list[int] = []
    real_passphrase = backup_mod.require_passphrase
    real_resolve = backup_mod.resolve_wordpress_now
    real_config = database_mod.WpConfigCredentials.resolve
    real_drive = backup_mod.drive_backup
    bound: list[Any] = []

    def passphrase(**kwargs: object) -> object:
        order.append(f"passphrase as {ids.uid}")
        return real_passphrase(**kwargs)  # type: ignore[arg-type]

    def resolve(*args: object, **kwargs: object) -> object:
        order.append(f"credentials as {ids.uid}")
        return real_resolve(*args, **kwargs)  # type: ignore[arg-type]

    def config_resolve(self: object) -> object:
        reads_as.append(ids.uid)
        return real_config(self)  # type: ignore[arg-type]

    def drive(*args: object, **kwargs: object) -> object:
        order.append(f"drive as {ids.uid}")
        return real_drive(*args, **kwargs)  # type: ignore[arg-type]

    def database(credentials: object, **_: object) -> FakeDatabase:
        bound.append(credentials)
        return fake_database

    monkeypatch.setattr(backup_mod, "require_passphrase", passphrase)
    monkeypatch.setattr(backup_mod, "resolve_wordpress_now", resolve)
    monkeypatch.setattr(database_mod.WpConfigCredentials, "resolve", config_resolve)
    monkeypatch.setattr(backup_mod, "drive_backup", drive)
    monkeypatch.setattr(adapter_sets, "WpDatabase", database)
    monkeypatch.setenv("L3IO_WP_BACKUP_PASSPHRASE", PASSPHRASE)
    final = tmp_path / "out.tar.gz"
    argv = _argv(site, "--archive", str(final), "--encrypt-with-passphrase")
    assert main(argv) == 0
    assert order == ["passphrase as 0", "credentials as 0", "drive as 33"]
    assert ids.calls[:3] == [("setgroups", [33]), ("setgid", 33), ("setuid", 33)]
    assert os.environ["HOME"] == owner.home
    # wp-config.php's pair was read as root, once, and never after the switch.
    assert reads_as == [0]
    (source,) = bound
    assert source.source.startswith("chain(")
    resolved = source.resolve()
    assert (resolved.username, resolved.password) == ("wp", "not-a-real-secret")
    assert resolved.source == f"wp-config:{site / 'wp-config.php'}"
    assert reads_as == [0]
    events = [e["event"] for e in pipeline_events(caplog)]
    assert "backup_runs_as_site_owner" in events
    assert final.is_file()


def test_the_dry_run_names_the_chain_it_resolved_as_root(
    site: Path,
    tmp_path: Path,
    fake_database: FakeDatabase,
    kernel: tuple[Kernel, privileges.SiteOwner],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The plan names the same source as a Run that did not switch."""
    final = tmp_path / "out.tar.gz"
    assert main(["--whatIf", *_argv(site, "--archive", str(final))]) == 0
    out = capsys.readouterr().out
    assert f"resolve credentials from chain(wp-config:{site / 'wp-config.php'})" in out
    assert not final.exists()


def test_a_files_backup_reads_no_credentials_as_root(
    site: Path,
    tmp_path: Path,
    fake_database: FakeDatabase,
    kernel: tuple[Kernel, privileges.SiteOwner],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-2 holds as root: a ``files`` backup touches no credential."""

    def resolve(*_: object, **__: object) -> object:
        raise AssertionError

    monkeypatch.setattr(backup_mod, "resolve_wordpress_now", resolve)
    final = tmp_path / "out.tar.gz"
    assert main(_argv(site, "--archive", str(final), "--mode", "files")) == 0
    assert kernel[0].uid == 33
    assert final.is_file()


def test_a_credential_failure_as_root_stops_before_the_switch(
    site: Path,
    tmp_path: Path,
    fake_database: FakeDatabase,
    kernel: tuple[Kernel, privileges.SiteOwner],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("L3IO_WP_BACKUP_DB_USER", "half")
    monkeypatch.delenv("L3IO_WP_BACKUP_DB_PASSWORD", raising=False)
    code = main(_argv(site, "--archive", str(tmp_path / "out.tar.gz")))
    assert code == int(ExitCode.CREDENTIALS)
    assert kernel[0].calls == []
    assert "L3IO_WP_BACKUP_DB_PASSWORD" in capsys.readouterr().err


def test_a_root_owned_site_root_is_refused_before_anything_is_read(
    site: Path,
    tmp_path: Path,
    fake_database: FakeDatabase,
    kernel: tuple[Kernel, privileges.SiteOwner],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        privileges, "site_owner", lambda _root: privileges.SiteOwner(0, 0, "root", "/")
    )

    def passphrase(**_: object) -> object:
        raise AssertionError

    monkeypatch.setattr(backup_mod, "require_passphrase", passphrase)
    argv = _argv(site, "--archive", str(tmp_path / "o.tar.gz"))
    code = main([*argv, "--encrypt-with-passphrase"])
    assert code == int(ExitCode.USAGE)
    assert "refused to back up as root" in capsys.readouterr().err
    assert kernel[0].calls == []
    assert fake_database.calls == []


@pytest.mark.parametrize("dry_run", [False, True], ids=["run", "dry-run"])
@pytest.mark.parametrize("chosen", ["archive", "archive-dir", "data-dir"])
def test_an_archive_directory_the_owner_cannot_write_is_refused_before_the_run(
    site: Path,
    tmp_path: Path,
    fake_database: FakeDatabase,
    kernel: tuple[Kernel, privileges.SiteOwner],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    dry_run: bool,
    chosen: str,
) -> None:
    backups = tmp_path / "backups"
    backups.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    if chosen == "archive":
        named, denied = ["--archive", str(backups / "a.tar.gz")], backups
    elif chosen == "archive-dir":
        named, denied = ["--archive-dir", str(backups)], backups
    else:
        # From the docroot: the owner's data directory, not made yet, is
        # checked where it would be made.
        monkeypatch.chdir(site)
        named, denied = [], data
    real_access = os.access

    def access(path: str, mode: int, **kwargs: object) -> bool:
        if Path(path) == denied:
            return False
        return real_access(path, mode, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "access", access)
    argv = [*(["--whatIf"] if dry_run else []), *_argv(site, *named)]
    code = main(argv)
    err = capsys.readouterr().err
    assert code == int(ArchiveDirectoryNotWritableError.exit_code)
    assert "is not writable by the Site Root's owner, www-data (uid 33)" in err
    expected = data / "l3io-wp-backup" / "archives" if chosen == "data-dir" else backups
    assert str(expected) in err
    assert kernel[0].uid == 33  # refused after the switch
    events = [e["event"] for e in pipeline_events(caplog)]
    assert "run_started" not in events
    assert fake_database.calls == []
    assert list(backups.iterdir()) == []


def test_without_root_nothing_switches_and_nothing_is_checked(
    site: Path,
    tmp_path: Path,
    fake_database: FakeDatabase,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(privileges, "running_as_root", lambda: False)

    def resolve(*_: object, **__: object) -> object:
        raise AssertionError

    monkeypatch.setattr(backup_mod, "resolve_wordpress_now", resolve)
    final = tmp_path / "out.tar.gz"
    assert main(_argv(site, "--archive", str(final))) == 0
    events = [e["event"] for e in pipeline_events(caplog)]
    assert "backup_runs_as_site_owner" not in events


# --------------------------------------------------------------------------
# Owner decision 68: the library refuses a backup as root
# --------------------------------------------------------------------------


def test_a_library_backup_as_root_is_refused_before_the_lock(
    site: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(privileges, "running_as_root", lambda: True)
    lock = RecordingSiteLock()
    request = BackupRequest(
        site_root=site,
        scope=BackupScope.ALL,
        target=None,
        wp_config=None,
        credentials=None,
        cipher=None,
        archive_dir=tmp_path,
    )
    with pytest.raises(RootBackupRefusedError) as caught:
        drive_backup(request, ctx=None, site_lock=lock)
    assert lock.events == []
    assert caught.value.exit_code is ExitCode.USAGE
    assert caught.value.arguments == ("site_root",)
    assert caught.value.reason == privileges.LIBRARY_RUNS_AS_HOST


def test_api_backup_as_root_is_refused_and_writes_nothing(
    site: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(privileges, "running_as_root", lambda: True)
    target = tmp_path / "out.tar.gz"
    with pytest.raises(api.RootBackupRefusedError) as caught:
        api.backup(site, scope=BackupScope.FILES, target=target)
    assert "drop privileges to the Site Root's owner first" in str(caught.value)
    assert not target.exists()
