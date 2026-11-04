"""AD-12, AD-5 — the Safety Snapshot's scope per Mode and target (E003-S01-001)."""

from __future__ import annotations

import dataclasses

import pytest

from l3io.wp.backup.core.restore import NoConfigForCutoverError, snapshot_scope
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.restore import (
    RestoreMode,
    RestorePhase,
    restores_database,
    restores_files,
)
from tests.restore_builders import EMPTY_DB, POPULATED, plan

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
    if not target.config_exists and not restores_files(mode):
        # E003-S02-005: a database Mode with no config to cut over refuses.
        assert restores_database(mode)
        with pytest.raises(NoConfigForCutoverError):
            plan(mode, target=target)
        return
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
