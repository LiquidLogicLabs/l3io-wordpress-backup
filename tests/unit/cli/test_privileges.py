"""Owner decisions 58, 67, 68 — a Run started as root runs as the Site Root's owner.

The switch itself, over a recording ``IdSystem`` that models the kernel's ids,
so these hold as root and as anyone: ``setgroups``, ``setgid``, ``setuid`` in
that order; every id read back; regaining root must fail; ``HOME`` becomes the
owner's. Refused: a root-owned Site Root, and a library backup or restore as
root, each with its operation's error. And the restore CLI root's order:
everything only root may read is read before the switch (the backup root's is
``tests/unit/cli/test_root_backup.py``).
The real switch, in a child process, is ``tests/integration/test_root_restore.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from structlog.testing import capture_logs

from l3io.wp.backup.cli import privileges
from l3io.wp.backup.cli.privileges import SiteOwner, drop_to_site_owner
from l3io.wp.backup.domain.errors import (
    PrivilegeDropError,
    RootBackupRefusedError,
    RootRestoreRefusedError,
    SiteRootNotFoundError,
)
from l3io.wp.backup.domain.exit_codes import ExitCode
from tests.root_mode import Kernel

OWNER = SiteOwner(uid=33, gid=34, user="www-data", home="/var/www")


@pytest.fixture
def as_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(privileges, "running_as_root", lambda: True)
    monkeypatch.setattr(privileges, "site_owner", lambda _root: OWNER)


@pytest.mark.usefixtures("as_root")
def test_the_switch_is_setgroups_setgid_setuid_in_that_order() -> None:
    kernel = Kernel()
    environ = {"HOME": "/root"}
    with capture_logs() as logs:
        owner = drop_to_site_owner(
            Path("/srv/site"), environ=environ, operation="restore", ids=kernel
        )
    assert owner == OWNER
    assert kernel.calls == [
        ("setgroups", [34]),
        ("setgid", 34),
        ("setuid", 33),
        ("setuid", 0),  # the proof that root cannot be regained
    ]
    assert (kernel.uid, kernel.gid, kernel.groups) == (33, 34, [34])
    assert environ["HOME"] == "/var/www"
    (record,) = [e for e in logs if e["event"] == "restore_runs_as_site_owner"]
    assert (record["uid"], record["gid"], record["user"]) == (33, 34, "www-data")


@pytest.mark.usefixtures("as_root")
@pytest.mark.parametrize(
    ("sticky", "step"),
    [
        ("uid", "the user ids did not change"),
        ("gid", "the group ids did not change"),
        ("groups", "supplementary groups remain"),
    ],
)
def test_a_switch_that_did_not_hold_stops_the_run(sticky: str, step: str) -> None:
    with pytest.raises(PrivilegeDropError) as caught:
        drop_to_site_owner(
            Path("/srv/site"),
            environ={},
            operation="restore",
            ids=Kernel(sticky=sticky),
        )
    assert caught.value.step == step
    assert caught.value.exit_code is ExitCode.REFUSED


@pytest.mark.usefixtures("as_root")
def test_root_that_could_be_regained_stops_the_run() -> None:
    with pytest.raises(PrivilegeDropError, match="root could be regained"):
        drop_to_site_owner(
            Path("/srv/site"), environ={}, operation="restore", ids=Kernel(regain=True)
        )


def test_a_root_owned_site_root_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(privileges, "running_as_root", lambda: True)
    monkeypatch.setattr(
        privileges, "site_owner", lambda _root: SiteOwner(0, 0, "root", "/root")
    )
    kernel = Kernel()
    with pytest.raises(RootRestoreRefusedError) as caught:
        drop_to_site_owner(
            Path("/srv/site"), environ={}, operation="restore", ids=kernel
        )
    assert caught.value.reason == privileges.ROOT_OWNED_SITE_ROOT
    assert caught.value.exit_code is ExitCode.USAGE
    assert kernel.calls == []


def test_not_root_switches_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(privileges, "running_as_root", lambda: False)
    kernel = Kernel()
    environ = {"HOME": "/home/me"}
    assert (
        drop_to_site_owner(
            Path("/srv/site"), environ=environ, operation="restore", ids=kernel
        )
        is None
    )
    assert kernel.calls == []
    assert environ == {"HOME": "/home/me"}


def test_an_owner_without_a_passwd_entry_keeps_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(privileges, "running_as_root", lambda: True)
    monkeypatch.setattr(
        privileges, "site_owner", lambda _root: SiteOwner(5001, 5001, None, None)
    )
    environ = {"HOME": "/root"}
    drop_to_site_owner(
        Path("/srv/site"), environ=environ, operation="restore", ids=Kernel()
    )
    assert environ == {"HOME": "/root"}


def test_the_site_owner_is_the_canonical_roots(tmp_path: Path) -> None:
    site = tmp_path / "site"
    site.mkdir()
    link = tmp_path / "link"
    link.symlink_to(site)
    owner = privileges.site_owner(link)
    assert (owner.uid, owner.gid) == (site.stat().st_uid, site.stat().st_gid)
    with pytest.raises(SiteRootNotFoundError):
        privileges.site_owner(tmp_path / "missing")


def test_the_library_refuses_a_restore_as_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(privileges, "running_as_root", lambda: True)
    with pytest.raises(RootRestoreRefusedError) as caught:
        privileges.require_not_root(Path("/srv/site"), operation="restore")
    assert caught.value.reason == privileges.LIBRARY_RUNS_AS_HOST
    monkeypatch.setattr(privileges, "running_as_root", lambda: False)
    privileges.require_not_root(Path("/srv/site"), operation="restore")


@pytest.mark.usefixtures("as_root")
def test_a_backup_switch_logs_its_own_event() -> None:
    """Owner decision 67: the same switch, recorded as the backup's."""
    kernel = Kernel()
    with capture_logs() as logs:
        drop_to_site_owner(
            Path("/srv/site"), environ={}, operation="backup", ids=kernel
        )
    assert (kernel.uid, kernel.gid) == (33, 34)
    events = [e["event"] for e in logs]
    assert "backup_runs_as_site_owner" in events
    assert "restore_runs_as_site_owner" not in events


@pytest.mark.parametrize(
    ("operation", "error", "verb"),
    [
        ("backup", RootBackupRefusedError, "back up"),
        ("restore", RootRestoreRefusedError, "restore"),
    ],
)
def test_each_operation_refuses_with_its_own_error(
    monkeypatch: pytest.MonkeyPatch,
    operation: privileges.Operation,
    error: type[Exception],
    verb: str,
) -> None:
    """A root-owned Site Root, and the library as root (owner decision 68)."""
    monkeypatch.setattr(privileges, "running_as_root", lambda: True)
    monkeypatch.setattr(
        privileges, "site_owner", lambda _root: SiteOwner(0, 0, "root", "/root")
    )
    with pytest.raises(error) as owned:
        privileges.owner_to_run_as(Path("/srv/site"), operation=operation)
    assert f"refused to {verb} as root" in str(owned.value)
    with pytest.raises(error) as library:
        privileges.require_not_root(Path("/srv/site"), operation=operation)
    assert getattr(library.value, "reason") == privileges.LIBRARY_RUNS_AS_HOST  # noqa: B009
    assert "drop privileges" in str(library.value)


# --------------------------------------------------------------------------
# The CLI root: read first, then switch
# --------------------------------------------------------------------------


def test_the_cli_reads_everything_before_it_switches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from l3io.wp.backup.cli import main
    from l3io.wp.backup.cli import restore as restore_mod
    from tests.restore_root_rig import Rig

    rig = Rig(tmp_path, monkeypatch)
    order: list[str] = []
    password_file = tmp_path / "pw"
    password_file.write_text("sentinel-relocated-pw-7c\n", encoding="utf-8")

    real_open, real_passphrase, real_password = (
        restore_mod._open_archive,  # noqa: SLF001
        restore_mod.resolve_passphrase,
        restore_mod.new_db_password,
    )

    def spy(name: str, real: object) -> object:
        def call(*args: object, **kwargs: object) -> object:
            order.append(name)
            return real(*args, **kwargs)  # type: ignore[operator]

        return call

    def switch(site_root: Path, *, environ: object, operation: str) -> None:
        assert operation == "restore"
        order.append("switch")

    def drive(*args: object, **kwargs: object) -> object:
        order.append("drive")
        raise KeyboardInterrupt

    monkeypatch.setattr(restore_mod, "_open_archive", spy("archive", real_open))
    monkeypatch.setattr(
        restore_mod, "resolve_passphrase", spy("passphrase", real_passphrase)
    )
    monkeypatch.setattr(restore_mod, "new_db_password", spy("password", real_password))
    monkeypatch.setattr(privileges, "drop_to_site_owner", switch)
    monkeypatch.setattr(restore_mod, "drive_restore", drive)
    main(
        [
            "restore",
            "--wp-dir",
            str(rig.site_root),
            "--archive",
            str(rig.archive_path),
            "--new-db-user",
            "u2",
            "--new-db-password-file",
            str(password_file),
        ]
    )
    assert order == ["password", "passphrase", "archive", "switch", "drive"]
