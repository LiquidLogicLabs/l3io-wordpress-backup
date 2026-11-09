"""AD-12, AD-5 — the Safety Snapshot's scope per Mode and target (E003-S01-001)."""

from __future__ import annotations

import dataclasses

import pytest

from l3io.wp.backup.core.restore import (
    NonEmptyTargetError,
    TargetChangedError,
    recheck_target,
    snapshot_scope,
)
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.restore import (
    OverrideSet,
    RestoreMode,
    RestorePhase,
    SnapshotPolicy,
    restores_database,
    restores_files,
)
from tests.restore_builders import EMPTY_DB, POPULATED, WAIVED, plan

TARGETS = {
    "full": POPULATED,
    "no-files": dataclasses.replace(
        POPULATED, site_root_empty=True, site_root_entries=0, config_exists=False
    ),
    "no-tables": dataclasses.replace(POPULATED, database=EMPTY_DB),
    "no-database": dataclasses.replace(POPULATED, database=None),
    "empty": dataclasses.replace(
        POPULATED,
        site_root_empty=True,
        site_root_entries=0,
        database=None,
        config_exists=False,
    ),
}

FILE_PHASES = {RestorePhase.CLEAN, RestorePhase.EXTRACT}


def _covers(scope: BackupScope | None) -> set[str]:
    return {
        None: set(),
        BackupScope.FILES: {"files"},
        BackupScope.DATABASE: {"database"},
        BackupScope.ALL: {"files", "database"},
    }[scope]


@pytest.mark.parametrize("mode", list(RestoreMode))
@pytest.mark.parametrize("name", sorted(TARGETS))
def test_the_scope_covers_every_populated_part_the_plan_destroys(
    mode: RestoreMode, name: str
) -> None:
    target = TARGETS[name]
    scope = snapshot_scope(mode, target)
    # Owner decision 49: no cutover without a relocation, so a database Mode
    # with no config to write is planned like any other.
    built = plan(mode, target=target)
    destroyed = set()
    if FILE_PHASES & set(built.phases) and not target.site_root_empty:
        destroyed.add("files")
    db = target.database
    populated_db = db is not None and db.database_exists and db.table_count > 0
    if RestorePhase.LOAD_DUMP in built.phases and populated_db:
        destroyed.add("database")
    assert _covers(scope) == destroyed
    assert built.snapshot_scope == scope
    assert (RestorePhase.SNAPSHOT in built.phases) is (scope is not None)


@pytest.mark.parametrize("mode", list(RestoreMode))
def test_none_only_when_every_touched_part_is_empty(mode: RestoreMode) -> None:
    assert snapshot_scope(mode, TARGETS["empty"]) is None
    expected = {
        (True, True): BackupScope.ALL,
        (True, False): BackupScope.FILES,
        (False, True): BackupScope.DATABASE,
    }[(restores_files(mode), restores_database(mode))]
    assert snapshot_scope(mode, TARGETS["full"]) is expected


# --------------------------------------------------------------------------
# RT-003 / ADV-4 — recheck_target against a fresh survey (E003-S02-003)
# --------------------------------------------------------------------------


def _buildable(mode: RestoreMode, target_name: str) -> bool:
    target = TARGETS[target_name]
    return target.config_exists or restores_files(mode)


@pytest.mark.parametrize("mode", list(RestoreMode))
@pytest.mark.parametrize("planned", sorted(TARGETS))
@pytest.mark.parametrize("fresh", sorted(TARGETS))
def test_a_fresh_survey_is_refused_exactly_when_it_outgrew_the_snapshot(
    mode: RestoreMode, planned: str, fresh: str
) -> None:
    if not _buildable(mode, planned):
        return
    built = plan(mode, target=TARGETS[planned])
    grown = _covers(snapshot_scope(mode, TARGETS[fresh])) - _covers(
        built.snapshot_scope
    )
    if grown:
        with pytest.raises(TargetChangedError) as caught:
            recheck_target(built, TARGETS[fresh])
        assert caught.value.found.value in grown
    else:
        recheck_target(built, TARGETS[fresh])


@pytest.mark.parametrize("mode", list(RestoreMode))
def test_under_skip_only_fr7_is_rechecked(mode: RestoreMode) -> None:
    if not _buildable(mode, "empty"):
        return
    empty = TARGETS["empty"]
    recheck_target(plan(mode, target=empty, snapshot=SnapshotPolicy.SKIP), POPULATED)
    unwaived = plan(
        mode,
        target=empty,
        snapshot=SnapshotPolicy.SKIP,
        overrides=OverrideSet(waived=frozenset()),
    )
    with pytest.raises(NonEmptyTargetError):
        recheck_target(unwaived, POPULATED)


def test_the_plan_carries_its_waivers() -> None:
    built = plan(RestoreMode.ALL_CLEAN, overrides=WAIVED)
    assert built.overrides == WAIVED
