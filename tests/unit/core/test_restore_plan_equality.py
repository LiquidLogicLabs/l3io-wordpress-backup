"""AD-4 — a restore plan is equal across the real and the Dry Run adapter sets.

Per ``RestoreMode`` (parametrised from the enum), the target is read through
the query halves of the real set (fakes behind it) and of
``dry_run_adapters(real)``; the two plans built from those reads, and their
rendered action lists, are equal. Driving the executor under the Dry Run set
then changes nothing on the Site.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from l3io.wp.backup.adapters.noop import ActionLog
from l3io.wp.backup.cli.adapter_sets import dry_run_adapters
from l3io.wp.backup.core.restore import TargetState, build_restore_plan
from l3io.wp.backup.domain.database import DatabaseLocation, DatabaseTargetState
from l3io.wp.backup.domain.restore import (
    RestoreMode,
    clears_site_root,
    restores_files,
)
from l3io.wp.backup.ports.adapter_set import AdapterSet
from tests.restore_harness import Harness, write_v1


class SurveyQuery:
    """The database query half: a populated WordPress database."""

    def survey_target(
        self,
        location: DatabaseLocation,
        *,
        table_prefix: str,
        wordpress_user: str | None,
    ) -> DatabaseTargetState:
        return DatabaseTargetState(True, 12, True, "57155", True)


def _target(adapters: AdapterSet, h: Harness) -> TargetState:
    site_files = adapters.site_files_query
    location = DatabaseLocation(h.site.db_host, h.site.db_port, h.site.db_name)
    return TargetState(
        site_root_empty=site_files.is_empty(h.site_root),
        database=adapters.restore_database_query.survey_target(
            location, table_prefix="wp_", wordpress_user=None
        ),
        config_exists=site_files.file_exists(h.config_path),
        site_root=h.site.site_root,
        site_root_entries=site_files.entry_count(h.site_root),
        database_location=location,
        load_target=None,
        load_location=None,
    )


def _rendered(h: Harness, adapters: AdapterSet) -> tuple[object, list[str]]:
    plan = build_restore_plan(
        h.site,
        h.mode,
        h.overrides,
        h.view,
        _target(adapters, h),
        archive=h.archive,
        relocation=h.relocation,
        snapshot=h.snapshot,
        snapshot_dir=h.work / "snapshots",
        config_dir=h.site_root,
        config_policy=h.config_policy,
    )
    return plan, [f"{a.ordinal}. {a.text}" for a in plan.actions]


def _tree(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


@pytest.mark.parametrize("mode", list(RestoreMode))
def test_the_real_and_dry_run_plans_are_equal(
    tmp_path: Path, mode: RestoreMode
) -> None:
    h = Harness(
        work=tmp_path, archive_path=write_v1(tmp_path), mode=mode, real_files=True
    )
    real = dataclasses.replace(h.adapters(), restore_database_query=SurveyQuery())  # type: ignore[arg-type]
    skipped = ActionLog()
    rehearsal = dry_run_adapters(real, log=skipped)
    assert _rendered(h, real) == _rendered(h, rehearsal)

    before = _tree(h.site_root)
    plan, _ = _rendered(h, rehearsal)
    report = h.run(plan=plan, adapters=rehearsal, journal=rehearsal.journal_command)  # type: ignore[arg-type]
    assert _tree(h.site_root) == before
    assert report.phases == plan.phases  # type: ignore[attr-defined]
    assert h.journal.events == []
    methods = {c.method for c in skipped.calls}
    assert ("extract" in methods) is restores_files(mode)
    assert ("clean" in methods) is clears_site_root(mode)
