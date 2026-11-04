"""FR-28/AD-19 — the Site Lock outlives neither SIGTERM nor SIGKILL.

Unmarked: no database is needed. The CLI runs a real backup in a subprocess
whose database double blocks inside the dump (the first phase), having said
so through a ready file, while it holds the Site Lock.

- SIGTERM: the Run's ``finally`` chain runs — exit ``FAILED``, the workspace
  removed, the holder record gone — and the lock is acquirable at once with
  no stale warning.
- SIGKILL: nothing runs, but the kernel freed the ``flock``; the next acquire
  succeeds and logs ``stale_lock_reclaimed`` naming the dead Run.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from structlog.testing import capture_logs

pytest.importorskip("l3io.wp.database")

from l3io.wp.backup.adapters.lock import FcntlSiteLock, default_lock_dir
from l3io.wp.backup.cli import _sigterm_raises
from l3io.wp.backup.domain.errors import RunTerminated
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.domain.lock import LockHolder

_CHILD = """
import os, sys, time
from pathlib import Path
from l3io.wp.backup.cli import adapter_sets, main
from l3io.wp.backup.domain.database import Readiness

site, archive, ready = sys.argv[1], sys.argv[2], Path(sys.argv[3])

class Blocking:
    def check_dump(self, location):
        return Readiness(ready=True)

    def dump_to(self, location, destination):
        ready.write_text(str(os.getpid()), encoding="utf-8")
        while True:
            time.sleep(0.05)

adapter_sets.WpDatabase = lambda credentials, **kwargs: Blocking()
sys.exit(main(["backup", "--wp-dir", site, "--archive", archive]))
"""

_TIMEOUT = 30.0


@pytest.fixture
def site(tmp_path: Path) -> Path:
    root = tmp_path / "www"
    root.mkdir()
    (root / "wp-config.php").write_text(
        "<?php\n"
        "define('DB_NAME', 'wptest');\n"
        "define('DB_USER', 'wp');\n"
        "define('DB_PASSWORD', 'not-a-real-secret');\n"
        "define('DB_HOST', 'db.invalid');\n"
        "$table_prefix = 'wp_';\n",
        encoding="utf-8",
    )
    (root / "index.php").write_text("<?php\n", encoding="utf-8")
    return root


def _start(site: Path, tmp_path: Path) -> tuple[subprocess.Popen[bytes], Path]:
    work = tmp_path / "work"
    work.mkdir()
    ready = tmp_path / "ready"
    child = subprocess.Popen(  # noqa: S603 - a fixed script under this interpreter
        [
            sys.executable,
            "-c",
            _CHILD,
            str(site),
            str(tmp_path / "out.tar.gz"),
            str(ready),
        ],
        env={**os.environ, "TMPDIR": str(work)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + _TIMEOUT
    while not ready.exists():
        if child.poll() is not None or time.monotonic() > deadline:
            child.kill()
            _, err = child.communicate()
            pytest.fail(f"the child never reached the dump: {err.decode()[-2000:]}")
        time.sleep(0.02)
    return child, work


def _records() -> list[Path]:
    return sorted(default_lock_dir().glob("*.json"))


def _holder() -> LockHolder:
    return LockHolder(
        run_id="next-run",
        pid=os.getpid(),
        started_at=datetime.now(UTC),
        hostname="here",
    )


def test_sigterm_releases_the_lock_and_exits_failed(site: Path, tmp_path: Path) -> None:
    child, work = _start(site, tmp_path)
    assert len(_records()) == 1, "the Run did not hold the lock"
    child.send_signal(signal.SIGTERM)
    _, err = child.communicate(timeout=_TIMEOUT)
    assert child.returncode == int(ExitCode.FAILED), err.decode()[-2000:]
    assert b"terminated by SIGTERM" in err
    assert list(work.iterdir()) == [], "the workspace survived"
    assert _records() == [], "the holder record survived"
    with capture_logs() as logs, FcntlSiteLock().acquire(site, _holder()):
        pass
    assert "stale_lock_reclaimed" not in [e["event"] for e in logs]


def test_sigkill_leaves_a_lock_the_next_run_reclaims(
    site: Path, tmp_path: Path
) -> None:
    child, _ = _start(site, tmp_path)
    (record,) = _records()
    dead = json.loads(record.read_text(encoding="utf-8"))
    child.kill()
    child.communicate(timeout=_TIMEOUT)
    assert child.returncode == -signal.SIGKILL
    assert _records() == [record], "SIGKILL ran a finally"
    with capture_logs() as logs, FcntlSiteLock().acquire(site, _holder()):
        pass
    stale = [e for e in logs if e["event"] == "stale_lock_reclaimed"]
    assert len(stale) == 1
    assert stale[0]["previous_run_id"] == dead["run_id"]
    assert stale[0]["previous_pid"] == child.pid
    assert stale[0]["previous_pid_alive"] is False


def _previous(signum: int, frame: object) -> None:
    del signum, frame


def test_the_sigterm_handler_lasts_only_for_the_run() -> None:
    before = signal.signal(signal.SIGTERM, _previous)
    try:
        installed: list[object] = []

        def terminated() -> None:
            with _sigterm_raises():
                installed.append(signal.getsignal(signal.SIGTERM))
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(1)

        with pytest.raises(RunTerminated, match="SIGTERM"):
            terminated()
        assert installed
        assert installed[0] is not _previous
        assert signal.getsignal(signal.SIGTERM) is _previous
    finally:
        signal.signal(signal.SIGTERM, before)


def test_off_the_main_thread_nothing_is_installed() -> None:
    seen: list[object] = []

    def body() -> None:
        with _sigterm_raises():
            seen.append(signal.getsignal(signal.SIGTERM))

    before = signal.getsignal(signal.SIGTERM)
    worker = threading.Thread(target=body)
    worker.start()
    worker.join()
    assert seen == [before]
