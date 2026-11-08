"""Close-out owner decision 2026-11-07, item 4 (BL-E002-065, BL-E003-086).

Each Run sweeps this user's stale ``l3io-wp-backup-*`` workspaces and its
target's stray ``.partial`` files older than 24 h, logging each removal by
path; a fresh one (a live Run's) is never touched. SIGHUP and SIGQUIT are
handled like SIGTERM: ``RunTerminated``, so the Run's cleanup runs.
"""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path, PurePosixPath

import pytest
from structlog.testing import capture_logs

from l3io.wp.backup import cli as cli_root
from l3io.wp.backup.adapters.store import LocalArchiveStore
from l3io.wp.backup.adapters.workspace import (
    STALE_AFTER_SECONDS,
    WORKSPACE_PREFIX,
    sweep_stale_workspaces,
)
from l3io.wp.backup.cli.context import open_run
from l3io.wp.backup.domain.errors import RunTerminated
from l3io.wp.backup.domain.storage_target import StorageTarget

OLD = time.time() - STALE_AFTER_SECONDS - 60


def _age(path: Path, when: float = OLD) -> None:
    for root, dirs, files in os.walk(path, topdown=False):
        for name in (*dirs, *files):
            os.utime(Path(root, name), (when, when))
    os.utime(path, (when, when))


def _workspace(parent: Path, name: str, *, stale: bool) -> Path:
    path = parent / f"{WORKSPACE_PREFIX}{name}"
    path.mkdir(mode=0o700)
    (path / "dump.sql").write_text("plaintext", encoding="utf-8")
    if stale:
        _age(path)
    return path


def test_a_run_sweeps_stale_workspaces_and_keeps_live_ones(tmp_path: Path) -> None:
    stale = _workspace(tmp_path, "dead", stale=True)
    live = _workspace(tmp_path, "live", stale=False)
    other = tmp_path / "unrelated"
    other.mkdir()
    _age(other)
    with (
        capture_logs() as logs,
        open_run(operation="backup", workspace_parent=tmp_path),
    ):
        pass
    assert not stale.exists()
    assert live.exists()
    assert other.exists()
    removed = [e for e in logs if e["event"] == "stale_residue_removed"]
    assert [e["path"] for e in removed] == [str(stale)]


def test_a_recent_file_inside_an_old_workspace_keeps_it(tmp_path: Path) -> None:
    path = _workspace(tmp_path, "busy", stale=True)
    os.utime(path / "dump.sql", None)  # a live Run still writing its Dump
    assert sweep_stale_workspaces(tmp_path, now=time.time()) == ()
    assert path.exists()


def test_a_symlinked_workspace_name_is_never_followed(tmp_path: Path) -> None:
    target = tmp_path / "precious"
    target.mkdir()
    (target / "keep").write_text("x", encoding="utf-8")
    link = tmp_path / f"{WORKSPACE_PREFIX}link"
    link.symlink_to(target)
    _age(target)
    os.utime(link, (OLD, OLD), follow_symlinks=False)
    assert sweep_stale_workspaces(tmp_path, now=time.time()) == ()
    assert (target / "keep").exists()


def _verify(path: Path) -> None:
    del path


def test_staging_sweeps_stale_partials_of_its_own_target(tmp_path: Path) -> None:
    final = tmp_path / "site.tar.gz"
    stale = tmp_path / ".site.tar.gz.abc123.partial"
    default_named = tmp_path / ".wpbackup2-wp-20260101T000000Z.tar.gz.x1.partial"
    fresh = tmp_path / ".site.tar.gz.def456.partial"
    foreign = tmp_path / ".other.tar.gz.zz.partial"
    for path in (stale, default_named, fresh, foreign):
        path.write_bytes(b"plaintext")
    for path in (stale, default_named, foreign):
        os.utime(path, (OLD, OLD))
    store = LocalArchiveStore(verify=_verify)
    with capture_logs() as logs:
        staged = store.stage(StorageTarget(scheme="file", path=PurePosixPath(final)))
    staged.discard()
    assert not stale.exists()
    assert not default_named.exists()
    assert fresh.exists()
    assert foreign.exists()
    paths = {e["path"] for e in logs if e["event"] == "stale_residue_removed"}
    assert paths == {str(stale), str(default_named)}


@pytest.mark.parametrize("name", ["SIGTERM", "SIGHUP", "SIGQUIT"])
def test_each_terminating_signal_raises_run_terminated(name: str) -> None:
    if not hasattr(signal, name):
        pytest.skip(f"{name} does not exist on this platform")
    sig = getattr(signal, name)
    before = signal.getsignal(sig)
    with cli_root._sigterm_raises(), pytest.raises(RunTerminated):  # noqa: SLF001
        os.kill(os.getpid(), sig)  # the handler raises here
    assert signal.getsignal(sig) == before


def test_the_signal_set_is_term_hup_and_quit() -> None:
    assert {s.name for s in cli_root.TERMINATING_SIGNALS} >= {"SIGTERM"}
    if hasattr(signal, "SIGHUP"):
        assert {"SIGHUP", "SIGQUIT"} <= {s.name for s in cli_root.TERMINATING_SIGNALS}
