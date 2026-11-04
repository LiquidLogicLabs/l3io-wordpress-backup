"""AD-12 — the snapshot directory may not be web-served (E003-S01-001)."""

from __future__ import annotations

from pathlib import PurePosixPath

import pytest

from l3io.wp.backup.core.restore import UnsafeSnapshotLocationError, snapshot_location
from l3io.wp.backup.domain.errors import ExitCode
from l3io.wp.backup.domain.restore import RestoreMode, RestorePhase
from tests.restore_builders import plan

ROOT = PurePosixPath("/srv/www/site")
PARENT = PurePosixPath("/srv/www")


@pytest.mark.parametrize(
    ("config_dir", "requested"),
    [
        (ROOT, ROOT / "snapshots"),
        (ROOT, ROOT),
        (PARENT, PARENT / "snapshots"),
        (PARENT, PARENT),
        # A symlink into the Site Root, as the caller resolves it.
        (ROOT, ROOT / "wp-content" / "uploads"),
    ],
    ids=["inside-root", "equal-root", "inside-config-dir", "equal-config-dir", "link"],
)
def test_a_web_served_location_is_refused(
    config_dir: PurePosixPath, requested: PurePosixPath
) -> None:
    with pytest.raises(UnsafeSnapshotLocationError) as caught:
        snapshot_location(ROOT, config_dir, requested)
    assert caught.value.exit_code is ExitCode.REFUSED


def test_the_per_user_default_is_accepted() -> None:
    default = PurePosixPath("/home/op/.local/state/l3io-wp-backup/snapshots/abc")
    assert snapshot_location(ROOT, PARENT, default) == default


def test_the_plan_refuses_an_unsafe_location_before_any_effect() -> None:
    with pytest.raises(UnsafeSnapshotLocationError):
        plan(RestoreMode.FILES, snapshot_dir=PurePosixPath("/srv/www/snaps"))


def test_skip_does_not_check_the_location() -> None:
    from l3io.wp.backup.domain.restore import SnapshotPolicy

    built = plan(
        RestoreMode.FILES,
        snapshot=SnapshotPolicy.SKIP,
        snapshot_dir=PurePosixPath("/srv/www/snaps"),
    )
    assert RestorePhase.SNAPSHOT not in built.phases
