"""FR-8 — restoring the reported Safety Snapshot gives back the pre-restore Site.

Unmarked, files only: a ``files-clean`` restore over a populated Site Root,
with the real ``LocalSiteFiles`` and a ``take_snapshot`` that joins E002's
``backup(..., ctx=ctx)`` inside the restore's Run; then a second
``files-clean`` restore from the reported snapshot path. The Site Root tree
(every path and its bytes) equals the one before the first restore.

The database half (BL-E003-047, E003-S02-006), marked ``integration``: a
``database`` restore through ``wpbackup2 restore`` over the CI database, then
a second one from the Safety Snapshot it reported; every table the live
database held has the same ``CHECKSUM TABLE`` in the database the Site runs on
afterwards.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from l3io.wp.backup.adapters.lock import FcntlSiteLock
from l3io.wp.backup.cli.adapter_sets import real_adapters
from l3io.wp.backup.cli.context import RunContext, open_run
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.domain.restore import RestoreMode, SnapshotPolicy
from tests.integration.live_cli import (
    CliSite,
    admin_account,  # noqa: F401 - a fixture
    authorizing_flags,
    cli_site,  # noqa: F401 - a fixture
    run_cli,
)
from tests.integration.live_restore import read_keys
from tests.restore_harness import Harness, joined_snapshot, write_v1


def _tree(root: Path) -> dict[str, bytes | None]:
    """Every entry under ``root``: a file's bytes, ``None`` for a directory."""
    tree: dict[str, bytes | None] = {}
    for directory, dirs, files in os.walk(root):
        for name in dirs:
            tree[os.path.relpath(Path(directory) / name, root)] = None
        for name in files:
            path = Path(directory) / name
            tree[os.path.relpath(path, root)] = path.read_bytes()
    return tree


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    (tmp_path / "archive").mkdir()
    (tmp_path / "run").mkdir()
    h = Harness(
        work=tmp_path / "run",
        archive_path=write_v1(tmp_path / "archive"),
        mode=RestoreMode.FILES_CLEAN,
        real_files=True,
    )
    h.snapshot_file.parent.mkdir(mode=0o700)
    uploads = h.site_root / "wp-content" / "uploads"
    uploads.mkdir(parents=True)
    (uploads / "photo.bin").write_bytes(os.urandom(4096))
    (h.site_root / "only-before.php").write_bytes(b"<?php // pre-restore only\n")
    return h


@pytest.fixture
def run(harness: Harness, tmp_path: Path) -> Iterator[RunContext]:
    with open_run(
        operation="restore",
        site_root=harness.site_root.resolve(),
        site_lock=FcntlSiteLock(lock_dir=tmp_path / "locks"),
        workspace_parent=tmp_path,
    ) as ctx:
        ctx.bind_adapters(
            real_adapters(
                wordpress_source=object(),  # type: ignore[arg-type]
                secrets=ctx.secrets,
                tracer=ctx.tracer,
            )
        )
        yield ctx


def test_restoring_the_snapshot_gives_back_the_pre_restore_tree(
    harness: Harness, run: RunContext, tmp_path: Path
) -> None:
    before = _tree(harness.site_root)
    harness.take_snapshot = joined_snapshot(harness, run)  # type: ignore[method-assign]
    with capture_logs():
        report = harness.run()
    assert report.snapshot is not None
    assert _tree(harness.site_root) != before
    assert not (harness.site_root / "only-before.php").exists()

    back = Harness(
        work=harness.work,
        archive_path=Path(report.snapshot.path),
        mode=RestoreMode.FILES_CLEAN,
        snapshot=SnapshotPolicy.SKIP,
        real_files=True,
    )
    with capture_logs():
        back.run()
    assert _tree(harness.site_root) == before


@pytest.mark.integration
@pytest.mark.skipif(
    "DB_HOST" not in os.environ, reason="needs the CI database (DB_HOST unset)"
)
def test_restoring_the_snapshot_gives_back_the_database(
    cli_site: CliSite,  # noqa: F811
    capsys: pytest.CaptureFixture[str],
) -> None:
    site = cli_site
    before = site.site.checksums()
    flags = authorizing_flags(RestoreMode.DATABASE)
    first = run_cli(site.argv(site.archive(), *flags), capsys)
    assert first.code == int(ExitCode.OK), first.err[-3000:]
    (written,) = first.events("safety_snapshot_written")
    snapshot = Path(written["path"])
    # Owner decision 49: the restore loaded the live database in place.
    assert str(read_keys(site.site.config)["DB_NAME"]) == site.site.database
    assert site.site.checksums() != before
    second = run_cli(site.argv(snapshot, *flags), capsys)
    assert second.code == int(ExitCode.OK), second.err[-3000:]
    # Decision 53: exactly the snapshot's tables, the Archive's extra gone.
    assert site.site.checksums() == before
