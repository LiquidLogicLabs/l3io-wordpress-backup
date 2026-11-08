"""BL-E008-027 — a per-user state directory another user could tamper with is refused.

Each user of the state directory (the Site Lock, the Run Journal, WP-CLI's
private directory) checks the directory it uses and every ancestor up to the
per-user state root: a real directory, owned by this process's effective
uid (owner decision 44), not group- or world-writable.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import pytest

from l3io.wp.backup.adapters import journal as journal_mod
from l3io.wp.backup.adapters import lock as lock_mod
from l3io.wp.backup.adapters.journal import FileRunJournal, JournalWriteError
from l3io.wp.backup.adapters.lock import FcntlSiteLock
from l3io.wp.backup.adapters.workspace import (
    STALE_AFTER_SECONDS,
    STATE_HOME_HINT,
    require_private_state_directory,
    sweep_stale,
)
from l3io.wp.backup.core.restore import NO_RELOCATION
from l3io.wp.backup.domain.errors import SiteLockLocationError
from l3io.wp.backup.domain.lock import LockHolder
from l3io.wp.backup.domain.restore import RestoreMode
from tests.staging_fakes import staged_header

pytestmark = pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX ownership")


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private state root, as ``state_root()`` for every module using it."""
    state = tmp_path / "state" / "l3io-wp-backup"
    state.mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(lock_mod, "state_root", lambda: state)
    monkeypatch.setattr(journal_mod, "state_root", lambda: state)
    return state


def test_a_private_tree_passes(root: Path) -> None:
    leaf = root / "snapshots" / "abc"
    leaf.mkdir(parents=True, mode=0o700)
    require_private_state_directory(leaf, root=root)


@pytest.mark.parametrize("where", ["root", "middle", "leaf"])
def test_a_group_or_world_writable_component_is_refused(root: Path, where: str) -> None:
    leaf = root / "snapshots" / "abc"
    leaf.mkdir(parents=True, mode=0o700)
    lax = {"root": root, "middle": root / "snapshots", "leaf": leaf}[where]
    lax.chmod(0o777)
    with pytest.raises(PermissionError, match="group- or world-writable"):
        require_private_state_directory(leaf, root=root)


def test_a_symlinked_component_is_refused(root: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "attacker"
    elsewhere.mkdir(mode=0o700)
    (root / "snapshots").symlink_to(elsewhere, target_is_directory=True)
    (elsewhere / "abc").mkdir(mode=0o700)
    with pytest.raises(NotADirectoryError, match="is a symlink"):
        require_private_state_directory(root / "snapshots" / "abc", root=root)


def test_a_component_another_user_owns_is_refused(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: root.stat().st_uid + 1)
    with pytest.raises(PermissionError, match="owned by another user"):
        require_private_state_directory(root, root=root)


def test_ownership_is_judged_by_the_effective_uid(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owner decision 44: the effective uid owns what the Run creates.

    ``sudo -E``: the real uid is the invoking user's, the effective uid
    root's. A state directory the real uid owns is refused; the refusal names
    both uids and points to ``XDG_STATE_HOME``.
    """
    owner = root.stat().st_uid
    monkeypatch.setattr(os, "getuid", lambda: owner)
    monkeypatch.setattr(os, "geteuid", lambda: owner + 1)
    with pytest.raises(PermissionError) as refused:
        require_private_state_directory(root, root=root)
    message = refused.value.strerror
    assert f"{str(root)!r} is owned by another user" in message
    assert f"uid {owner};" in message
    assert f"uid {owner + 1}" in message
    assert STATE_HOME_HINT in message
    assert "XDG_STATE_HOME" in STATE_HOME_HINT


def test_a_real_uid_that_differs_from_the_owner_is_not_judged(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = root.stat().st_uid
    monkeypatch.setattr(os, "getuid", lambda: owner + 1)
    monkeypatch.setattr(os, "geteuid", lambda: owner)
    require_private_state_directory(root, root=root)


def test_the_site_lock_refusal_names_xdg_state_home(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = tmp_path / "site"
    site.mkdir()
    monkeypatch.setattr(os, "geteuid", lambda: root.stat().st_uid + 1)
    holder = LockHolder(
        run_id="r", pid=os.getpid(), started_at=datetime.now(UTC), hostname="h"
    )
    with pytest.raises(SiteLockLocationError, match="XDG_STATE_HOME"):
        FcntlSiteLock(lock_dir=root / "locks").acquire(site, holder)


def test_the_sweep_judges_ownership_by_the_effective_uid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owner decision 44: the sweep removes only what the effective uid owns."""
    stale = tmp_path / "residue"
    stale.mkdir(mode=0o700)
    old = time.time() - STALE_AFTER_SECONDS - 60
    os.utime(stale, (old, old))
    owner = stale.stat().st_uid
    monkeypatch.setattr(os, "getuid", lambda: owner)
    monkeypatch.setattr(os, "geteuid", lambda: owner + 1)
    assert sweep_stale([stale], now=time.time()) == ()
    assert stale.exists()
    monkeypatch.setattr(os, "getuid", lambda: owner + 1)
    monkeypatch.setattr(os, "geteuid", lambda: owner)
    assert sweep_stale([stale], now=time.time()) == (stale,)
    assert not stale.exists()


def test_ancestors_above_the_root_are_not_judged(root: Path) -> None:
    """``/tmp``-like ancestors (sticky, world-writable) are not the state dir."""
    root.parent.chmod(0o777)
    try:
        require_private_state_directory(root, root=root)
    finally:
        root.parent.chmod(0o700)


def test_the_site_lock_refuses_a_lax_state_directory(
    root: Path, tmp_path: Path
) -> None:
    site = tmp_path / "site"
    site.mkdir()
    root.chmod(0o777)
    holder = LockHolder(
        run_id="r", pid=os.getpid(), started_at=datetime.now(UTC), hostname="h"
    )
    with pytest.raises(SiteLockLocationError, match="group- or world-writable"):
        FcntlSiteLock(lock_dir=root / "locks").acquire(site, holder)


def test_the_journal_refuses_a_symlinked_state_directory(
    root: Path, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "attacker"
    elsewhere.mkdir(mode=0o700)
    (root / "snapshots").symlink_to(elsewhere, target_is_directory=True)
    journal = FileRunJournal(root / "snapshots" / "abc")
    header = staged_header(
        run_id="run-1",
        site_root=PurePosixPath("/srv/www"),
        live="wp",
        relocation=NO_RELOCATION,
        mode=RestoreMode.ALL_CLEAN,
        archive="/backups/a.tar.gz",
        config_path="/srv/www/wp-config.php",
    )
    with pytest.raises(JournalWriteError):
        journal.begin_run(header)
    assert list(elsewhere.iterdir()) == []
