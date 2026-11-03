"""AD-19 — ``FcntlSiteLock``: one Run per Site, held outside the Site Root.

Real filesystem under ``tmp_path``, the lock directory injected. A second
acquire from a **separate process** (``multiprocessing`` spawn,
``tests/site_lock_holder.py``) refuses naming the holder; every spelling of a
Site — symlink, ``..``, trailing slash — takes one lock; the same Run
re-enters by depth; a lock directory inside the Site Root, or reached through
a symlink into it, is refused; file modes are ``0700``/``0600``.
"""

from __future__ import annotations

import json
import os
import socket
import stat
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import platformdirs
import pytest
from structlog.testing import capture_logs

from l3io.wp.backup.adapters import lock as lock_module
from l3io.wp.backup.adapters.lock import APP_NAME, FcntlSiteLock, default_lock_dir
from l3io.wp.backup.domain.errors import (
    InvalidSiteDescriptorError,
    SiteLockedError,
    SiteLockLocationError,
    SiteLockReleaseError,
)
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.domain.lock import LockHolder
from tests.site_lock_holder import HOLDER_HOST, HOLDER_RUN_ID, held_elsewhere

STARTED = datetime(2026, 11, 3, 9, 30, tzinfo=UTC)


def _holder(run_id: str = "run-a") -> LockHolder:
    return LockHolder(
        run_id=run_id, pid=os.getpid(), started_at=STARTED, hostname="here"
    )


@pytest.fixture
def site(tmp_path: Path) -> Path:
    root = tmp_path / "www"
    root.mkdir()
    return root


@pytest.fixture
def lock_dir(tmp_path: Path) -> Path:
    return tmp_path / "state" / "locks"


@pytest.fixture
def lock(lock_dir: Path) -> Iterator[FcntlSiteLock]:
    yield FcntlSiteLock(lock_dir)
    held = lock_module._REGISTRY  # noqa: SLF001 - leak check on the process registry
    assert not held, "a test left a Site Lock held"


def _files(lock_dir: Path) -> dict[str, Path]:
    return {p.suffix: p for p in lock_dir.iterdir() if not p.name.startswith(".")}


def test_acquire_and_release(lock: FcntlSiteLock, site: Path, lock_dir: Path) -> None:
    with capture_logs() as logs:
        handle = lock.acquire(site, _holder())
        assert handle.site_root == site.resolve()
        files = _files(lock_dir)
        record = json.loads(files[".json"].read_text(encoding="utf-8"))
        assert record == {
            "run_id": "run-a",
            "pid": os.getpid(),
            "started_at": "2026-11-03T09:30:00Z",
            "hostname": "here",
            "site_root": str(site.resolve()),
        }
        handle.release()
    assert set(_files(lock_dir)) == {".lock"}, "the holder record outlived the lock"
    events = [e["event"] for e in logs]
    assert events == ["site_lock_acquired", "site_lock_released"]
    acquired = logs[0]
    assert acquired["log_level"] == "info"
    assert acquired["site_root"] == str(site.resolve())
    assert acquired["lock_path"] == str(files[".lock"])
    # Released: acquirable again, by another Run.
    with lock.acquire(site, _holder("run-b")):
        pass


def test_the_lock_file_name_is_the_hash_of_the_canonical_path(
    lock: FcntlSiteLock, site: Path, lock_dir: Path
) -> None:
    import hashlib

    with lock.acquire(site, _holder()):
        digest = hashlib.sha256(str(site.resolve()).encode()).hexdigest()
        assert set(_files(lock_dir).values()) == {
            lock_dir / f"{digest}.lock",
            lock_dir / f"{digest}.json",
        }


def test_modes(lock: FcntlSiteLock, site: Path, lock_dir: Path) -> None:
    with lock.acquire(site, _holder()):
        assert stat.S_IMODE(lock_dir.stat().st_mode) == 0o700
        for path in _files(lock_dir).values():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600, path


def test_a_lax_existing_directory_is_tightened(
    lock: FcntlSiteLock, site: Path, lock_dir: Path
) -> None:
    lock_dir.mkdir(parents=True, mode=0o755)
    lock_dir.chmod(0o755)
    with lock.acquire(site, _holder()):
        assert stat.S_IMODE(lock_dir.stat().st_mode) == 0o700


def test_another_process_holding_it_refuses_naming_the_holder(
    lock: FcntlSiteLock, site: Path, lock_dir: Path
) -> None:
    with (
        held_elsewhere(site, lock_dir) as holder,
        capture_logs() as logs,
        pytest.raises(SiteLockedError) as caught,
    ):
        lock.acquire(site, _holder())
    error = caught.value
    assert error.exit_code is ExitCode.LOCKED
    assert error.holder_run_id == HOLDER_RUN_ID
    assert error.holder_pid == holder.pid
    assert error.holder_started_at == "2026-11-03T12:00:00Z"
    assert error.holder_hostname == HOLDER_HOST
    assert error.site_root == site.resolve()
    assert error.lock_path.parent == lock_dir.resolve()
    for text in (HOLDER_RUN_ID, str(holder.pid), "2026-11-03T12:00:00Z"):
        assert text in str(error)
    refused = [e for e in logs if e["event"] == "site_locked"]
    assert len(refused) == 1
    assert refused[0]["log_level"] == "error"
    assert refused[0]["holder_run_id"] == HOLDER_RUN_ID
    assert refused[0]["holder_pid"] == holder.pid
    # The holder released on leaving: acquirable now, without a stale warning.
    with capture_logs() as after, lock.acquire(site, _holder()):
        pass
    assert "stale_lock_reclaimed" not in [e["event"] for e in after]


def test_a_different_site_proceeds(
    lock: FcntlSiteLock, tmp_path: Path, site: Path, lock_dir: Path
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    with held_elsewhere(site, lock_dir), lock.acquire(other, _holder()):
        pass


@pytest.mark.parametrize("spelling", ["symlink", "dotdot", "trailing_slash"])
def test_every_spelling_of_a_site_is_one_lock(
    lock: FcntlSiteLock, tmp_path: Path, site: Path, lock_dir: Path, spelling: str
) -> None:
    if spelling == "symlink":
        alias = tmp_path / "alias"
        alias.symlink_to(site, target_is_directory=True)
    elif spelling == "dotdot":
        (site / "sub").mkdir()
        alias = site / "sub" / ".."
    else:
        alias = Path(f"{site}/")
    with held_elsewhere(site, lock_dir), pytest.raises(SiteLockedError) as caught:
        lock.acquire(alias, _holder())
    assert caught.value.site_root == site.resolve()


def test_the_same_run_re_enters_by_depth(
    lock: FcntlSiteLock, tmp_path: Path, site: Path, lock_dir: Path
) -> None:
    alias = tmp_path / "alias"
    alias.symlink_to(site, target_is_directory=True)
    outer = lock.acquire(site, _holder())
    inner = lock.acquire(alias, _holder())
    inner.release()
    # Still held: the inner release did not unlock.
    with pytest.raises(SiteLockedError):
        FcntlSiteLock(lock_dir).acquire(site, _holder("run-b"))
    outer.release()
    with lock.acquire(site, _holder("run-b")):
        pass


def test_another_run_in_this_process_refuses(lock: FcntlSiteLock, site: Path) -> None:
    with lock.acquire(site, _holder()), pytest.raises(SiteLockedError) as caught:
        lock.acquire(site, _holder("run-b"))
    assert caught.value.holder_run_id == "run-a"


def test_an_unbalanced_release_raises(lock: FcntlSiteLock, site: Path) -> None:
    handle = lock.acquire(site, _holder())
    handle.release()
    with pytest.raises(SiteLockReleaseError):
        handle.release()


def test_a_double_release_of_an_inner_handle_raises(
    lock: FcntlSiteLock, site: Path
) -> None:
    outer = lock.acquire(site, _holder())
    inner = lock.acquire(site, _holder())
    inner.release()
    with pytest.raises(SiteLockReleaseError):
        inner.release()
    outer.release()


def test_a_stale_record_is_reclaimed_with_a_warning(
    lock: FcntlSiteLock, site: Path, lock_dir: Path
) -> None:
    with lock.acquire(site, _holder()):
        record = _files(lock_dir)[".json"]
    # A holder killed outright leaves its record; the kernel freed its flock.
    record.write_text(
        json.dumps(
            {
                "run_id": "dead-run",
                "pid": 2**22 + 1,
                "started_at": "2026-11-02T01:00:00Z",
                "hostname": socket.gethostname(),
                "site_root": str(site),
            }
        ),
        encoding="utf-8",
    )
    with capture_logs() as logs, lock.acquire(site, _holder()):
        current = json.loads(record.read_text(encoding="utf-8"))
    assert current["run_id"] == "run-a"
    stale = [e for e in logs if e["event"] == "stale_lock_reclaimed"]
    assert len(stale) == 1
    assert stale[0]["log_level"] == "warning"
    assert stale[0]["previous_run_id"] == "dead-run"
    assert stale[0]["previous_pid"] == 2**22 + 1
    assert stale[0]["previous_started_at"] == "2026-11-02T01:00:00Z"
    assert stale[0]["previous_pid_alive"] is False
    # Never the current Run's correlation key (AD-10).
    assert "run_id" not in stale[0]


@pytest.mark.parametrize("content", [b"{not json", b"[]", b'{"run_id": 1}', b""])
def test_an_unreadable_record_with_the_lock_held_still_refuses(
    lock: FcntlSiteLock, site: Path, lock_dir: Path, content: bytes
) -> None:
    with held_elsewhere(site, lock_dir):
        record = _files(lock_dir)[".json"]
        record.write_bytes(content)
        with pytest.raises(SiteLockedError) as caught:
            lock.acquire(site, _holder())
    error = caught.value
    assert error.holder_run_id is None
    assert "could not be identified" in str(error)
    assert str(error.lock_path) in str(error)


def test_a_missing_record_with_the_lock_held_still_refuses(
    lock: FcntlSiteLock, site: Path, lock_dir: Path
) -> None:
    with held_elsewhere(site, lock_dir):
        _files(lock_dir)[".json"].unlink()
        with pytest.raises(SiteLockedError, match="could not be identified"):
            lock.acquire(site, _holder())


def test_a_missing_site_root_creates_no_lock(tmp_path: Path, lock_dir: Path) -> None:
    with pytest.raises(InvalidSiteDescriptorError):
        FcntlSiteLock(lock_dir).acquire(tmp_path / "absent", _holder())
    assert not lock_dir.exists()


# --------------------------------------------------------------------------
# Location — scope attacks: inside the Site Root, by any route
# --------------------------------------------------------------------------


def test_a_lock_directory_inside_the_site_root_is_refused(site: Path) -> None:
    inside = site / "wp-content" / "locks"
    with pytest.raises(SiteLockLocationError) as caught:
        FcntlSiteLock(inside).acquire(site, _holder())
    assert caught.value.exit_code is ExitCode.NOT_READY
    assert not inside.exists(), "the refused directory was created"


def test_the_site_root_itself_is_refused(site: Path) -> None:
    with pytest.raises(SiteLockLocationError):
        FcntlSiteLock(site).acquire(site, _holder())


def test_a_symlink_into_the_site_root_is_refused(tmp_path: Path, site: Path) -> None:
    (site / "hidden").mkdir()
    link = tmp_path / "looks-outside"
    link.symlink_to(site / "hidden", target_is_directory=True)
    with pytest.raises(SiteLockLocationError):
        FcntlSiteLock(link).acquire(site, _holder())


def test_a_site_reached_through_a_symlink_still_sees_its_inside(
    tmp_path: Path, site: Path
) -> None:
    alias = tmp_path / "alias"
    alias.symlink_to(site, target_is_directory=True)
    with pytest.raises(SiteLockLocationError):
        FcntlSiteLock(site / "locks").acquire(alias, _holder())


def test_a_misconfigured_xdg_state_home_inside_the_site_is_refused(
    site: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(site / ".state"))
    with pytest.raises(SiteLockLocationError):
        FcntlSiteLock().acquire(site, _holder())


def test_an_uncreatable_directory_is_not_ready(tmp_path: Path, site: Path) -> None:
    blocker = tmp_path / "a-file"
    blocker.write_text("", encoding="utf-8")
    with pytest.raises(SiteLockLocationError) as caught:
        FcntlSiteLock(blocker / "locks").acquire(site, _holder())
    assert caught.value.exit_code is ExitCode.NOT_READY


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
def test_an_unwritable_directory_is_not_ready(tmp_path: Path, site: Path) -> None:
    parent = tmp_path / "ro"
    parent.mkdir()
    parent.chmod(0o500)
    try:
        with pytest.raises(SiteLockLocationError):
            FcntlSiteLock(parent / "locks").acquire(site, _holder())
    finally:
        parent.chmod(0o700)


def test_the_default_directory_is_the_per_user_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    expected = platformdirs.user_state_path(APP_NAME) / "locks"
    assert default_lock_dir() == expected
    assert APP_NAME == "l3io-wp-backup"


def test_the_default_lock_file_is_outside_the_site_root(site: Path) -> None:
    # The lock actually taken through the default directory, not the bare
    # path; a directory configured inside the Site is refused (tests above).
    with FcntlSiteLock().acquire(site, _holder()) as handle:
        assert handle.lock_path.parent == default_lock_dir().resolve()
        assert not handle.lock_path.is_relative_to(site.resolve())
