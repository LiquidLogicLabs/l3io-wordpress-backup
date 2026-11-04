"""FR-8 — restoring the reported Safety Snapshot gives back the pre-restore Site.

Unmarked, files only: a ``files-clean`` restore over a populated Site Root,
with the real ``LocalSiteFiles`` and a ``take_snapshot`` that joins E002's
``backup(..., ctx=ctx)`` inside the restore's Run; then a second
``files-clean`` restore from the reported snapshot path. The Site Root tree
(every path and its bytes) equals the one before the first restore. The
database half (``CHECKSUM TABLE``) needs the root's real ``take_snapshot``
binding over a live server, which E003-S02-006 builds (BL-E003-047).
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
from l3io.wp.backup.domain.restore import RestoreMode, SnapshotPolicy
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
