"""FR-6, AD-5, AD-17 — the restore plan, per Restore Mode (E003-S01-001).

Parametrised over ``list(RestoreMode)``: a Mode added to the enum without a
row here fails :data:`EXPECTED`'s completeness test rather than being skipped.
"""

from __future__ import annotations

import dataclasses

import pytest

from l3io.wp.backup.core.backup import DUMP_REQUIREMENT
from l3io.wp.backup.core.restore import (
    NO_RELOCATION,
    ArchiveHasNoConfigError,
    RestorePlan,
    RestoreRelocation,
    build_restore_plan,
)
from l3io.wp.backup.domain.errors import ArchiveHasNoDumpError, ExitCode
from l3io.wp.backup.domain.preflight import ToolRequirement
from l3io.wp.backup.domain.restore import (
    ConfigPolicy,
    RestoreMode,
    RestorePhase,
    SnapshotPolicy,
    is_subsequence,
)
from tests.restore_builders import (
    ARCHIVE,
    POPULATED,
    RELOCATIONS,
    SITE,
    SNAPSHOTS,
    URL_ONLY,
    WAIVED,
    plan,
    view,
)

P = RestorePhase
HEAD = (P.PREFLIGHT, P.VERIFY, P.SNAPSHOT)

#: FR-6's table, and nothing else: the phases each Mode's plan holds.
EXPECTED: dict[RestoreMode, tuple[RestorePhase, ...]] = {
    RestoreMode.FILES: (*HEAD, P.EXTRACT),
    RestoreMode.FILES_CLEAN: (*HEAD, P.CLEAN, P.EXTRACT),
    RestoreMode.DATABASE: (*HEAD, P.LOAD_DUMP),
    RestoreMode.DATABASE_CLEAN: (*HEAD, P.LOAD_DUMP),
    RestoreMode.ALL: (*HEAD, P.EXTRACT, P.LOAD_DUMP),
    RestoreMode.ALL_CLEAN: (*HEAD, P.CLEAN, P.EXTRACT, P.LOAD_DUMP),
}
#: FR-6's row per Mode: (files written, file removal, database written,
#: database removal).
ROWS: dict[RestoreMode, tuple[bool, bool, bool, bool]] = {
    RestoreMode.FILES: (True, False, False, False),
    RestoreMode.FILES_CLEAN: (True, True, False, False),
    RestoreMode.DATABASE: (False, False, True, False),
    RestoreMode.DATABASE_CLEAN: (False, False, True, True),
    RestoreMode.ALL: (True, False, True, False),
    RestoreMode.ALL_CLEAN: (True, True, True, True),
}


def test_every_mode_has_a_row() -> None:
    assert set(EXPECTED) == set(RestoreMode) == set(ROWS)


@pytest.mark.parametrize("mode", list(RestoreMode))
def test_the_plan_is_exactly_the_modes_phases(mode: RestoreMode) -> None:
    built = plan(mode)
    assert built.phases == EXPECTED[mode]
    assert is_subsequence(built.phases)
    assert built.mode is mode


@pytest.mark.parametrize("mode", list(RestoreMode))
def test_the_plan_touches_only_what_the_mode_names(mode: RestoreMode) -> None:
    files, clean, database, drop = ROWS[mode]
    built = plan(mode)
    assert (P.EXTRACT in built.phases) is files
    assert (P.CLEAN in built.phases) is clean
    assert (P.LOAD_DUMP in built.phases) is database
    loads = [a for a in built.actions if a.phase is P.LOAD_DUMP]
    assert [a.fields["drop_previous"] for a in loads] == ([drop] if database else [])
    assert all("drop" not in a.text for a in loads) is not drop


def test_two_swapped_phases_are_refused() -> None:
    built = plan(RestoreMode.ALL_CLEAN)
    swapped = (P.PREFLIGHT, P.VERIFY, P.SNAPSHOT, P.EXTRACT, P.CLEAN, P.LOAD_DUMP)
    with pytest.raises(ValueError, match="subsequence"):
        RestorePlan(
            mode=built.mode,
            phases=swapped,
            snapshot=built.snapshot,
            snapshot_scope=built.snapshot_scope,
            overrides=built.overrides,
            required_tools=(),
            actions=(),
        )


@pytest.mark.parametrize("mode", list(RestoreMode))
def test_skip_removes_snapshot_from_phases_and_actions(mode: RestoreMode) -> None:
    built = plan(mode, snapshot=SnapshotPolicy.SKIP)
    assert P.SNAPSHOT not in built.phases
    assert all(a.phase is not P.SNAPSHOT for a in built.actions)


@pytest.mark.parametrize("mode", list(RestoreMode))
def test_actions_are_numbered_in_phase_order(mode: RestoreMode) -> None:
    built = plan(mode)
    assert [a.ordinal for a in built.actions] == list(range(1, len(built.actions) + 1))
    assert tuple(dict.fromkeys(a.phase for a in built.actions)) == built.phases
    assert built.actions[1].text == f"verify {ARCHIVE.path}"
    assert built.actions[2].text.startswith("safety snapshot of ")


@pytest.mark.parametrize("mode", [m for m in RestoreMode if ROWS[m][2]])
def test_a_database_mode_without_a_dump_is_refused(mode: RestoreMode) -> None:
    with pytest.raises(ArchiveHasNoDumpError) as caught:
        plan(mode, archive_view=view(dump=False))
    assert caught.value.exit_code is ExitCode.REFUSED
    assert "scope 'files'" in str(caught.value)
    assert str(ARCHIVE.path) in str(caught.value)


@pytest.mark.parametrize("mode", [m for m in RestoreMode if not ROWS[m][2]])
def test_a_files_mode_needs_no_dump(mode: RestoreMode) -> None:
    assert plan(mode, archive_view=view(dump=False)).phases == EXPECTED[mode]


def test_overwrite_without_an_archive_config_is_refused() -> None:
    with pytest.raises(ArchiveHasNoConfigError) as caught:
        plan(
            RestoreMode.FILES,
            archive_view=view(config=False),
            config_policy=ConfigPolicy.OVERWRITE,
        )
    assert caught.value.exit_code is ExitCode.REFUSED


def test_overwrite_with_a_target_config_plans_rewrite_config() -> None:
    built = plan(RestoreMode.FILES, config_policy=ConfigPolicy.OVERWRITE)
    assert built.phases == (*HEAD, P.EXTRACT, P.REWRITE_CONFIG)
    no_target = dataclasses.replace(POPULATED, config_exists=False)
    bare = plan(
        RestoreMode.FILES, target=no_target, config_policy=ConfigPolicy.OVERWRITE
    )
    assert P.REWRITE_CONFIG not in bare.phases


# --------------------------------------------------------------------------
# Relocation rows (gap pass 2)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", list(RestoreMode))
@pytest.mark.parametrize("name", sorted(RELOCATIONS))
def test_relocation_keeps_every_plan_a_subsequence(
    mode: RestoreMode, name: str
) -> None:
    relocation = RELOCATIONS[name]
    if not ROWS[mode][2]:
        # Epic 7's validate_relocation refuses URL search-replace without a
        # database restore before planning; the plan refuses it too.
        relocation = dataclasses.replace(
            relocation,
            search_replace=None,
            undo=tuple(u for u in relocation.undo if u[0] is not P.SEARCH_REPLACE),
        )
    built = plan(mode, relocation=relocation)
    assert is_subsequence(built.phases)
    assert P.SEARCH_REPLACE not in built.phases or P.LOAD_DUMP in built.phases
    if RELOCATIONS[name] is NO_RELOCATION:
        assert P.REWRITE_CONFIG not in built.phases
        assert P.SEARCH_REPLACE not in built.phases


def test_relocation_tools_reach_the_plan_after_its_own() -> None:
    extra = ToolRequirement("wp-cli", (2, 10), "the caller")
    built = plan(RestoreMode.ALL, relocation=URL_ONLY)
    assert [t.name for t in built.required_tools] == ["database-dump", "wp-cli"]
    assert built.required_tools[0] == DUMP_REQUIREMENT
    merged = build_restore_plan(
        SITE,
        RestoreMode.ALL,
        WAIVED,
        view(),
        POPULATED,
        archive=ARCHIVE,
        relocation=URL_ONLY,
        snapshot=SnapshotPolicy.TAKE,
        snapshot_dir=SNAPSHOTS,
        config_dir=SITE.site_root,
        config_policy=ConfigPolicy.KEEP,
        extra_tools=(extra,),
    )
    assert merged.required_tools[1].minimum_version == (2, 10)


def test_a_files_only_snapshot_needs_no_dump_tool() -> None:
    assert plan(RestoreMode.FILES).required_tools == ()


def test_relocation_actions_follow_their_phase() -> None:
    relocation = dataclasses.replace(
        URL_ONLY, actions=((P.REWRITE_CONFIG, "rewrite WP_HOME"),)
    )
    built = plan(RestoreMode.ALL, relocation=relocation)
    texts = [(a.phase, a.text) for a in built.actions]
    at = texts.index((P.REWRITE_CONFIG, "rewrite WP_HOME"))
    assert texts[at - 1][0] is P.REWRITE_CONFIG
    assert texts[at + 1][0] is P.LOAD_DUMP


@pytest.mark.parametrize(
    "relocation",
    [
        dataclasses.replace(URL_ONLY, actions=((P.CLEAN, "x"),)),
        dataclasses.replace(URL_ONLY, undo=((P.CLEAN, "x"),)),
        dataclasses.replace(URL_ONLY, undo=URL_ONLY.undo[1:]),
        dataclasses.replace(URL_ONLY, undo=URL_ONLY.undo[:1]),
    ],
    ids=["action-omitted-phase", "undo-omitted-phase", "no-rewrite-undo", "no-sr-undo"],
)
def test_a_malformed_relocation_is_a_programming_error(
    relocation: RestoreRelocation,
) -> None:
    with pytest.raises(ValueError, match="relocation"):
        plan(RestoreMode.ALL, relocation=relocation)


@pytest.mark.parametrize("key", ["DB_HOST", "DB_USER", "DB_PASSWORD", "DB_NAME"])
def test_a_cutover_key_in_rewrite_values_is_unrepresentable(key: str) -> None:
    with pytest.raises(ValueError, match=key):
        dataclasses.replace(NO_RELOCATION, rewrite_values=((key, "x"),))


def test_action_lines_never_name_a_username() -> None:
    relocation = RELOCATIONS["both"]
    built = plan(RestoreMode.ALL_CLEAN, relocation=relocation)
    rendered = " ".join(a.text + repr(a.fields) for a in built.actions)
    assert relocation.cutover_user is not None
    assert relocation.cutover_user not in rendered
    load = next(a for a in built.actions if a.phase is P.LOAD_DUMP)
    assert load.fields["host"] == "db2.internal"
    assert load.fields["database"] == "moved_db"
