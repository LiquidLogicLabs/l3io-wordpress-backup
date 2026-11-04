"""FR-8, AD-16, AD-19 — the Safety Snapshot joins the restore's Run (E003-S02-003).

``take_snapshot`` here is bound the way E003-S02-006's root binds it: E002's
``backup(..., ctx=ctx)`` inside the restore's Run, then
:func:`~l3io.wp.backup.adapters.restore_runner.snapshot_result` over the
returned ``BackupResult``'s fields. With a recording ``SiteLock`` the joined
path acquires nothing for the Run's own Site Root, and a variant that opens a
second Run instead of joining is refused (``RunContextActiveError``) at once —
a nested Run is an error, never a hang.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from l3io.wp.backup.adapters.tar import verify_archive
from l3io.wp.backup.cli.adapter_sets import real_adapters
from l3io.wp.backup.cli.context import RunContext, open_run
from l3io.wp.backup.core.restore import SnapshotFailedError, SnapshotResult
from l3io.wp.backup.domain.archive_format import (
    Integrity,
)
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.errors import ExitCode, RunContextActiveError
from l3io.wp.backup.domain.restore import RestoreMode
from tests.restore_harness import Harness, joined_snapshot, write_v1
from tests.unit.cli.conftest import RecordingSiteLock

pytestmark = pytest.mark.usefixtures("fake_database", "workdir")


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    (tmp_path / "archive").mkdir()
    (tmp_path / "run").mkdir()
    archive = write_v1(tmp_path / "archive")
    h = Harness(
        work=tmp_path / "run", archive_path=archive, mode=RestoreMode.FILES_CLEAN
    )
    h.snapshot_file.parent.mkdir(mode=0o700)
    return h


@pytest.fixture
def recorder() -> RecordingSiteLock:
    return RecordingSiteLock()


@pytest.fixture
def run(harness: Harness, recorder: RecordingSiteLock) -> Iterator[RunContext]:
    """The restore's Run, holding the harness Site's lock."""
    with open_run(
        operation="restore", site_root=harness.site_root.resolve(), site_lock=recorder
    ) as ctx:
        ctx.bind_adapters(
            real_adapters(
                wordpress_source=object(),  # type: ignore[arg-type]
                secrets=ctx.secrets,
                tracer=ctx.tracer,
            )
        )
        yield ctx


def test_the_joined_snapshot_acquires_nothing_and_is_verified(
    harness: Harness, run: RunContext, recorder: RecordingSiteLock
) -> None:
    before = list(recorder.events)
    harness.take_snapshot = joined_snapshot(harness, run)  # type: ignore[method-assign]
    with capture_logs():
        report = harness.run()
    assert recorder.events == before
    assert report.snapshot is not None
    assert report.snapshot.performed is True
    assert report.snapshot.integrity is Integrity.VERIFIED
    path = Path(report.snapshot.path)
    assert not path.resolve().is_relative_to(harness.site_root.resolve())
    assert verify_archive(path).integrity is Integrity.VERIFIED


def test_a_snapshot_that_opens_a_second_run_is_refused_at_once(
    harness: Harness, run: RunContext, recorder: RecordingSiteLock
) -> None:
    def nested(scope: BackupScope) -> SnapshotResult:
        with open_run(
            operation="backup",
            site_root=harness.site_root.resolve(),
            site_lock=recorder,
        ):
            pass
        msg = "unreachable: open_run refuses inside a Run"
        raise AssertionError(msg)

    harness.take_snapshot = nested  # type: ignore[method-assign]
    with capture_logs(), pytest.raises(SnapshotFailedError) as caught:
        harness.run()
    assert caught.value.exit_code is ExitCode.REFUSED
    assert isinstance(caught.value.cause, RunContextActiveError)
    assert caught.value.cause.active_run_id == run.run_id
    destroyed = {"site_files.clean", "site_files.extract"}
    assert not destroyed & set(harness.log.methods())
