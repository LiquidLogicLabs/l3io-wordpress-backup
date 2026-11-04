"""AD-12, AD-5 — the Safety Snapshot's scope per Mode and target (E003-S01-001)."""

from __future__ import annotations

import pytest

from l3io.wp.backup.core.restore import TargetState, snapshot_scope
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.restore import (
    RestoreMode,
    RestorePhase,
    restores_database,
    restores_files,
)
from tests.restore_builders import EMPTY_DB, FULL_DB, plan

TARGETS = {
    "full": TargetState(site_root_empty=False, database=FULL_DB, config_exists=True),
    "no-files": TargetState(
        site_root_empty=True, database=FULL_DB, config_exists=False
    ),
    "no-tables": TargetState(
        site_root_empty=False, database=EMPTY_DB, config_exists=True
    ),
    "no-database": TargetState(
        site_root_empty=False, database=None, config_exists=True
    ),
    "empty": TargetState(site_root_empty=True, database=None, config_exists=False),
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
