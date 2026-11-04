"""FR-8, AD-16, AD-19 — the Safety Snapshot joins the restore's Run: no deadlock.

Unmarked: the ``files-clean`` restore needs no database (the ``files`` scope
contacts none). The destructive ``all-clean`` variant over the CI database
(BL-E003-047, E003-S02-006) is ``python -m wpbackup2 restore`` itself, with
the root's ``load_staged`` slot and real ``take_snapshot``; it is marked
``integration`` and skips without ``DB_HOST``. A subprocess
runs ``execute_restore`` inside a Run opened by ``open_run`` with the
**real** ``FcntlSiteLock`` and a ``take_snapshot`` that joins E002's
``backup(..., ctx=ctx)`` (:func:`tests.restore_harness.joined_snapshot`),
under JSON logging. ``subprocess.run(timeout=)`` turns a deadlock into
``TimeoutExpired``, never a hang.

- one ``run_id``, one ``run_started``, one ``run_finished``, one
  ``site_lock_acquired``; every ``phase_completed`` carries that ``run_id``;
- the reported snapshot exists outside the Site Root and verifies
  ``VERIFIED``; the lock is acquirable at once after exit;
- paused inside ``SNAPSHOT``, the Site is ``LOCKED`` to a second acquirer
  spelling the Site Root through a symlink, naming the first Run.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from l3io.wp.backup.adapters.lock import FcntlSiteLock
from l3io.wp.backup.adapters.tar import verify_archive
from l3io.wp.backup.core.backup import BackupStep
from l3io.wp.backup.domain.archive_format import Integrity
from l3io.wp.backup.domain.errors import SiteLockedError
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.domain.lock import LockHolder
from l3io.wp.backup.domain.restore import RestoreMode, RestorePhase
from tests.integration.live_cli import (
    CliSite,
    admin_account,  # noqa: F401 - a fixture
    authorizing_flags,
    cli_site,  # noqa: F401 - a fixture
)

pytest.importorskip("l3io.wp.database")

_ROOT = Path(__file__).resolve().parents[2]
_TIMEOUT = 120

_CHILD = """
import json, sys, time
from pathlib import Path
from l3io.wp.backup.adapters.lock import FcntlSiteLock
from l3io.wp.backup.cli.adapter_sets import real_adapters
from l3io.wp.backup.cli.context import open_run
from l3io.wp.backup.cli.logging import configure_logging
from l3io.wp.backup.domain.restore import RestoreMode
from tests.restore_harness import Harness, joined_snapshot, write_v1

work, locks, pause = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
configure_logging("INFO", "json")
(work / "archive").mkdir()
(work / "run").mkdir()
h = Harness(
    work=work / "run",
    archive_path=write_v1(work / "archive"),
    mode=RestoreMode.FILES_CLEAN,
)
h.snapshot_file.parent.mkdir(mode=0o700)
with open_run(
    operation="restore",
    site_root=h.site_root.resolve(),
    site_lock=FcntlSiteLock(lock_dir=locks),
) as ctx:
    ctx.bind_adapters(
        real_adapters(wordpress_source=object(), secrets=ctx.secrets, tracer=ctx.tracer)
    )
    joined = joined_snapshot(h, ctx)

    def take(scope):
        if pause:
            (work / "ready").write_text(ctx.run_id, encoding="utf-8")
            while not (work / "go").exists():
                time.sleep(0.05)
        return joined(scope)

    h.take_snapshot = take
    report = h.run()
    out = {"snapshot": str(report.snapshot.path), "site_root": str(h.site_root)}
print(json.dumps(out))
"""


def _child(tmp_path: Path, *, pause: bool) -> tuple[list[str], Path, dict[str, str]]:
    work = tmp_path / "work"
    work.mkdir()
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    args = [sys.executable, "-c", _CHILD, str(work), str(tmp_path / "locks")]
    args.append("pause" if pause else "")
    env = {**os.environ, "TMPDIR": str(tmp), "PYTHONPATH": str(_ROOT)}
    return args, work, env


def _records(stderr: str) -> list[dict[str, object]]:
    records = []
    for line in stderr.splitlines():
        if line.startswith("{"):
            records.append(json.loads(line))
    return records


def _holder() -> LockHolder:
    return LockHolder(
        run_id="second-run",
        pid=os.getpid(),
        started_at=datetime.now(UTC),
        hostname=socket.gethostname(),
    )


def test_the_snapshot_joins_one_run_and_the_lock_is_free_after(tmp_path: Path) -> None:
    args, _work, env = _child(tmp_path, pause=False)
    done = subprocess.run(  # noqa: S603 - a fixed script under this interpreter
        args,
        env=env,
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT,
        check=False,
    )
    assert done.returncode == ExitCode.OK, done.stderr[-3000:]
    records = _records(done.stderr)
    events = [r["event"] for r in records]
    assert events.count("run_started") == 1
    assert events.count("run_finished") == 1
    assert events.count("site_lock_acquired") == 1
    run_ids = {r["run_id"] for r in records if "run_id" in r}
    assert len(run_ids) == 1
    # The harness verifies its fixture Archive before the Run opens.
    inside = records[events.index("run_started") :]
    phases = {str(r["phase"]) for r in inside if r["event"] == "phase_completed"}
    assert {"snapshot", BackupStep.WRITE_ARCHIVE.value} <= phases
    assert all(r.get("run_id") in run_ids for r in inside)
    out = json.loads(done.stdout.strip().splitlines()[-1])
    snapshot, site_root = Path(out["snapshot"]), Path(out["site_root"])
    assert snapshot.exists()
    assert not snapshot.resolve().is_relative_to(site_root.resolve())
    assert verify_archive(snapshot).integrity is Integrity.VERIFIED
    with FcntlSiteLock(lock_dir=tmp_path / "locks").acquire(site_root, _holder()):
        pass


def test_the_lock_is_held_across_the_snapshot(tmp_path: Path) -> None:
    args, work, env = _child(tmp_path, pause=True)
    child = subprocess.Popen(  # noqa: S603 - a fixed script under this interpreter
        args,
        env=env,
        cwd=_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + _TIMEOUT
        ready = work / "ready"
        while not ready.exists():
            if child.poll() is not None or time.monotonic() > deadline:
                child.kill()
                _, err = child.communicate()
                pytest.fail(f"the child never reached SNAPSHOT: {err[-3000:]}")
            time.sleep(0.05)
        first_run = ready.read_text(encoding="utf-8")
        spelled = tmp_path / "spelled"
        spelled.symlink_to(work / "run" / "target")
        lock = FcntlSiteLock(lock_dir=tmp_path / "locks")
        with pytest.raises(SiteLockedError) as caught:
            lock.acquire(spelled, _holder())
        assert caught.value.exit_code is ExitCode.LOCKED
        assert first_run in str(caught.value)
        (work / "go").write_text("", encoding="utf-8")
        _, err = child.communicate(timeout=_TIMEOUT)
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate()
    assert child.returncode == ExitCode.OK, err[-3000:]


# --------------------------------------------------------------------------
# BL-E003-047: the database half, through the real root over the CI database
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(
    "DB_HOST" not in os.environ, reason="needs the CI database (DB_HOST unset)"
)
def test_a_destructive_all_clean_restore_joins_one_run(
    cli_site: CliSite,  # noqa: F811
) -> None:
    """``all-clean`` with ``load_staged``: one Run, snapshot and load, no deadlock."""
    site = cli_site
    original = site.site.database
    argv = [
        sys.executable,
        "-m",
        "wpbackup2",
        *site.argv(site.archive(), *authorizing_flags(RestoreMode.ALL_CLEAN)),
    ]
    done = subprocess.run(  # noqa: S603 - a fixed module under this interpreter
        argv,
        env={**os.environ, "PYTHONPATH": str(_ROOT)},
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT,
        check=False,
    )
    assert done.returncode == ExitCode.OK, done.stderr[-3000:]
    records = _records(done.stderr)
    events = [r["event"] for r in records]
    assert events.count("run_started") == 1
    assert events.count("run_finished") == 1
    assert events.count("site_lock_acquired") == 1
    assert len({r["run_id"] for r in records if "run_id" in r}) == 1
    phases = {str(r["phase"]) for r in records if r["event"] == "phase_completed"}
    assert {
        RestorePhase.SNAPSHOT.value,
        RestorePhase.LOAD_DUMP.value,
        BackupStep.WRITE_ARCHIVE.value,
    } <= phases
    (written,) = [r for r in records if r["event"] == "safety_snapshot_written"]
    snapshot = Path(written["path"])
    assert not snapshot.resolve().is_relative_to(site.root.resolve())
    assert verify_archive(snapshot).integrity is Integrity.VERIFIED
    # The load cut over and the previous database was dropped (all-clean).
    assert original not in site.site.databases()
    with FcntlSiteLock().acquire(site.root, _holder()):
        pass
