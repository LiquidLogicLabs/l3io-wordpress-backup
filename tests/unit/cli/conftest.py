"""Fixtures for the Run Context and driver tests (E002-S01-004).

``site`` is a WordPress Site Root whose database host does not resolve;
``fake_database`` replaces the one ``WpDatabase`` ``real_adapters`` builds with
a double that writes a fixed Dump, so a whole backup runs without a server.
Every workspace is created under the test's own temporary directory.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

pytest.importorskip("l3io.wp.database")

from l3io.wp.backup.cli import adapter_sets
from l3io.wp.backup.domain.database import DatabaseLocation, DumpResult, Readiness
from l3io.wp.backup.domain.errors import DumpFailedError, SiteLockedError
from l3io.wp.backup.domain.lock import LockHolder

DUMP_TEXT = b"-- dump\n"


class RecordingSiteLock:
    """A ``SiteLock`` journaling ``acquire``/``release`` into ``events``.

    ``refuse`` makes ``acquire`` raise ``SiteLockedError`` naming a holder.
    """

    def __init__(self, events: list[object] | None = None) -> None:
        self.events: list[object] = [] if events is None else events
        self.refuse = False

    def acquire(self, site_root: Path, holder: LockHolder) -> RecordedHandle:
        self.events.append(("lock.acquire", site_root, holder.run_id))
        if self.refuse:
            raise SiteLockedError(
                site_root, Path("/state/x.lock"), holder_run_id="other", holder_pid=1
            )
        return RecordedHandle(self, site_root)


class RecordedHandle:
    def __init__(self, owner: RecordingSiteLock, site_root: Path) -> None:
        self.owner = owner
        self._site_root = site_root

    @property
    def site_root(self) -> Path:
        return self._site_root

    def release(self) -> None:
        self.owner.events.append(("lock.release", self._site_root))

    def __enter__(self) -> RecordedHandle:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


class FakeDatabase:
    """Both database halves: writes a fixed Dump at ``0600``, or fails."""

    def __init__(self) -> None:
        self.fail = False
        self.calls: list[Path] = []
        self.checks: list[DatabaseLocation] = []
        self.readiness = Readiness(ready=True)

    def check_dump(self, location: DatabaseLocation) -> Readiness:
        self.checks.append(location)
        return self.readiness

    def dump_to(self, location: DatabaseLocation, destination: Path) -> DumpResult:
        self.calls.append(destination)
        if self.fail:
            msg = "server went away"
            raise DumpFailedError(msg)
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(DUMP_TEXT)
        return DumpResult(bytes=len(DUMP_TEXT), duration_ms=1.0)


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The temporary directory every workspace is created in."""
    work = tmp_path / "tmp"
    work.mkdir()
    monkeypatch.setenv("TMPDIR", str(work))
    monkeypatch.setattr(tempfile, "tempdir", None)
    return work


@pytest.fixture
def site(tmp_path: Path, workdir: Path) -> Path:
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


@pytest.fixture
def fake_database(monkeypatch: pytest.MonkeyPatch) -> FakeDatabase:
    database = FakeDatabase()
    monkeypatch.setattr(
        adapter_sets, "WpDatabase", lambda credentials, *, secrets, admin=None: database
    )
    return database
