"""FR-7, AD-25 — the non-empty-target guard (E003-S01-002).

Scope from the table: every key of ``domain/flag_map.py::FLAG_MAP`` (itself
derived from the parser by ``tests/contracts/test_flag_map.py``), crossed with
four target states. The expected verdict is computed from the **flags**, not
from ``OverrideSet``: without ``--force`` every non-empty part the ``--mode``
names refuses; with it none does. So a table row whose waiver drifted from its
flag fails here.

A relocation's load database holding tables is refused first, whatever the
flags (owner decision 73, :class:`RelocationTargetNotEmptyError`): ``--force``
does not waive it.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.cli.exit import exit_code_for
from l3io.wp.backup.core.restore import (
    NonEmptyTargetError,
    RelocationTargetNotEmptyError,
    TargetState,
    check_target,
    render_flags,
)
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.database import DatabaseLocation, DatabaseTargetState
from l3io.wp.backup.domain.errors import FlagCombinationRejectedError
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.domain.flag_map import (
    FLAG_MAP,
    FlagCombination,
    FlagRejection,
    authorizing_combination,
    combination,
    resolve_flags,
)
from l3io.wp.backup.domain.restore import (
    Override,
    OverrideSet,
    RestoreMode,
    RestorePart,
    restores_database,
    restores_files,
)
from tests.conftest import CredentialSentinel
from tests.restore_builders import EMPTY, EMPTY_DB, FOREIGN_DB, POPULATED, SITE, plan
from tests.restore_builders import FULL_DB as WORDPRESS_DB
from tests.restore_harness import Harness, write_v1

#: Owner decision 57: only a database that does not look like this WordPress
#: site (tables, none with its prefix) needs the database waiver; the guard's
#: refusal cases are built over one. A WordPress database never refuses
#: (``test_a_wordpress_or_empty_database_needs_no_waiver``).
FULL_DB = FOREIGN_DB
POPULATED = dataclasses.replace(POPULATED, database=FOREIGN_DB)

LIVE = SITE.database_location()
LOAD = DatabaseLocation(host="db2.internal", port=3306, name="moved_db")

TARGETS: dict[str, TargetState] = {
    "empty": EMPTY,
    "files": dataclasses.replace(EMPTY, site_root_empty=False, site_root_entries=3),
    "database": dataclasses.replace(EMPTY, database=FULL_DB),
    "both": POPULATED,
    "load": dataclasses.replace(EMPTY, load_target=FULL_DB, load_location=LOAD),
    "live-and-load": dataclasses.replace(
        POPULATED, load_target=FULL_DB, load_location=LOAD
    ),
}

SCOPE_PARTS: dict[BackupScope, set[RestorePart]] = {
    BackupScope.FILES: {"files"},
    BackupScope.DATABASE: {"database"},
    BackupScope.ALL: {"files", "database"},
}


def _non_empty(target: TargetState) -> set[RestorePart]:
    parts: set[RestorePart] = set()
    if target.site_root_entries:
        parts.add("files")
    if _databases(target):
        parts.add("database")
    return parts


def _databases(target: TargetState) -> list[str]:
    """``host:port/name`` of the live database when it holds a table."""
    db, location = target.database, target.database_location
    if db is not None and db.table_count and location is not None:
        return [f"{location.host}:{location.port}/{location.name}"]
    return []


def _load_has_tables(target: TargetState) -> bool:
    load = target.load_target
    return load is not None and load.database_exists and load.table_count > 0


@pytest.mark.parametrize("target_name", list(TARGETS))
@pytest.mark.parametrize("flags", list(FLAG_MAP), ids=render_flags)
def test_every_table_row_refuses_or_proceeds_as_its_flags_say(
    flags: FlagCombination, target_name: str
) -> None:
    target = TARGETS[target_name]
    values = dict(flags)
    if isinstance(FLAG_MAP[flags], FlagRejection):
        # No Mode: the guard is unreachable for this row.
        with pytest.raises(FlagCombinationRejectedError):
            resolve_flags(flags)
        return
    mode, overrides = resolve_flags(flags)
    scope = values["mode"]
    assert isinstance(scope, BackupScope)
    if restores_database(mode) and _load_has_tables(target):
        with pytest.raises(RelocationTargetNotEmptyError) as refused:
            check_target(mode, overrides, target)
        assert refused.value.database == "db2.internal:3306/moved_db"
        assert refused.value.tables == 12
        return
    expected = set() if values["force"] else SCOPE_PARTS[scope] & _non_empty(target)
    if not expected:
        check_target(mode, overrides, target)
        return
    with pytest.raises(NonEmptyTargetError) as caught:
        check_target(mode, overrides, target)
    err = caught.value
    assert set(err.parts) == expected
    assert len(err.parts) == len(expected)
    assert err.mode is mode
    for part in err.parts:
        assert err.authorizing == authorizing_combination(mode, part)
    assert dict(err.authorizing)["force"] is True
    assert dict(err.authorizing)["mode"] is scope
    if "files" in expected:
        assert str(SITE.site_root) in err.found
        assert "3 entries" in err.detail
    if "database" in expected:
        for where in _databases(target):
            assert (where, "12 tables") in err.findings
    assert err.found == "; ".join(where for where, _ in err.findings)
    assert err.detail == "; ".join(what for _, what in err.findings)
    assert render_flags(err.authorizing) in str(err)


@pytest.mark.parametrize("mode", list(RestoreMode))
def test_one_combination_authorizes_every_part_a_mode_restores(
    mode: RestoreMode,
) -> None:
    """The refusal carries one ``authorizing``; it must waive every part named."""
    restores = {"files": restores_files, "database": restores_database}
    parts: list[RestorePart] = [p for p in ("files", "database") if restores[p](mode)]
    found = {authorizing_combination(mode, p) for p in parts}
    assert len(found) == 1
    _, overrides = resolve_flags(found.pop())
    assert all(overrides.waives(p) for p in parts)


TWO_PART_MODES = [m for m in RestoreMode if restores_files(m) and restores_database(m)]


def test_some_mode_touches_both_parts() -> None:
    assert TWO_PART_MODES


@pytest.mark.parametrize("mode", TWO_PART_MODES)
@pytest.mark.parametrize(
    ("waived", "refused"),
    [(Override.NONEMPTY_FILES, "database"), (Override.NONEMPTY_DATABASE, "files")],
)
def test_a_waiver_for_one_part_never_waives_the_other(
    mode: RestoreMode, waived: Override, refused: RestorePart
) -> None:
    with pytest.raises(NonEmptyTargetError) as caught:
        check_target(mode, OverrideSet(waived=frozenset({waived})), POPULATED)
    assert caught.value.parts == (refused,)


def test_both_parts_unauthorized_are_named_in_one_refusal() -> None:
    with pytest.raises(NonEmptyTargetError) as caught:
        check_target(RestoreMode.ALL, OverrideSet.none(), POPULATED)
    err = caught.value
    assert err.parts == ("files", "database")
    assert err.found == f"'{SITE.site_root}'; {LIVE.host}:{LIVE.port}/{LIVE.name}"
    none_of = "none of wp-config.php, wp-includes, wp-content"
    assert err.detail == f"3 entries, {none_of}; 12 tables"
    assert f"found '{SITE.site_root}' (3 entries, {none_of}); {LIVE.host}:" in str(err)
    assert err.authorizing == combination(
        mode=BackupScope.ALL, cleanfirst=False, drop_database=False, force=True
    )


def test_cleanfirst_without_force_on_a_non_empty_site_root_refuses_naming_force() -> (
    None
):
    """AD-25's worked case: the destruction is authorized, the guard is not."""
    mode, overrides = resolve_flags(
        combination(
            mode=BackupScope.FILES, cleanfirst=True, drop_database=False, force=False
        )
    )
    assert mode is RestoreMode.FILES_CLEAN
    with pytest.raises(NonEmptyTargetError) as caught:
        check_target(mode, overrides, TARGETS["files"])
    err = caught.value
    assert err.parts == ("files",)
    assert render_flags(err.authorizing) == "mode=files cleanfirst force"
    assert str(err).endswith("mode=files cleanfirst force would authorize it")


def test_a_non_empty_live_database_refuses_without_a_load_database() -> None:
    """The guard reads the live database, never the not-yet-created staging one."""
    target = dataclasses.replace(EMPTY, database=FULL_DB, load_target=None)
    with pytest.raises(NonEmptyTargetError) as caught:
        check_target(RestoreMode.DATABASE, OverrideSet.none(), target)
    assert caught.value.parts == ("database",)
    assert caught.value.found == f"{LIVE.host}:{LIVE.port}/{LIVE.name}"


@pytest.mark.parametrize("load", [FULL_DB, WORDPRESS_DB], ids=["foreign", "wordpress"])
@pytest.mark.parametrize("force", [False, True], ids=["plain", "force"])
def test_a_load_database_holding_tables_is_refused_even_with_force(
    load: DatabaseTargetState, force: bool
) -> None:
    """Owner decision 73: a relocation's target must be missing or empty; a
    WordPress database there is refused too, and ``--force`` waives nothing."""
    target = dataclasses.replace(
        EMPTY, database=EMPTY_DB, load_target=load, load_location=LOAD
    )
    overrides = OverrideSet(waived=frozenset(Override)) if force else OverrideSet.none()
    with pytest.raises(RelocationTargetNotEmptyError) as caught:
        check_target(RestoreMode.DATABASE, overrides, target)
    assert caught.value.database == "db2.internal:3306/moved_db"
    assert caught.value.tables == 12
    assert "db2.internal:3306/moved_db" in str(caught.value)
    assert "--force does not waive" in str(caught.value)
    assert exit_code_for(caught.value) is ExitCode.REFUSED


def test_a_load_location_that_is_the_live_database_is_not_a_relocation() -> None:
    """``--new-db-host`` naming the live server: the live database is loaded,
    and the Safety Snapshot covers it (decision 57's rule applies)."""
    target = dataclasses.replace(
        EMPTY, database=WORDPRESS_DB, load_target=WORDPRESS_DB, load_location=LIVE
    )
    check_target(RestoreMode.DATABASE, OverrideSet.none(), target)


def test_a_load_database_with_tables_is_ignored_by_a_files_restore() -> None:
    target = dataclasses.replace(EMPTY, load_target=FULL_DB, load_location=LOAD)
    check_target(RestoreMode.FILES, OverrideSet.none(), target)


@pytest.mark.parametrize(
    ("counts", "detail"),
    [
        ({"routine_count": 1}, "0 tables, 1 routine"),
        ({"event_count": 2}, "0 tables, 2 events"),
        ({"routine_count": 3, "event_count": 1}, "0 tables, 3 routines, 1 event"),
    ],
    ids=["routines-only", "events-only", "both"],
)
@pytest.mark.parametrize("which", ["live", "load"])
def test_a_database_with_only_routines_or_events_is_not_empty(
    counts: dict[str, int], detail: str, which: str
) -> None:
    """BL-E003-053: no tables or views, but stored code the load would destroy."""
    database = dataclasses.replace(EMPTY_DB, **counts)
    target = (
        dataclasses.replace(EMPTY, database=database, load_target=None)
        if which == "live"
        else dataclasses.replace(
            EMPTY, database=EMPTY_DB, load_target=database, load_location=LOAD
        )
    )
    with pytest.raises(NonEmptyTargetError) as caught:
        check_target(RestoreMode.DATABASE, OverrideSet.none(), target)
    assert caught.value.parts == ("database",)
    assert caught.value.detail == detail


@pytest.mark.parametrize(
    "database",
    [None, EMPTY_DB, dataclasses.replace(EMPTY_DB, database_exists=False)],
    ids=["no-survey", "no-tables", "absent"],
)
@pytest.mark.parametrize("mode", list(RestoreMode))
def test_an_empty_target_needs_no_override(
    mode: RestoreMode, database: DatabaseTargetState | None
) -> None:
    target = dataclasses.replace(EMPTY, database=database, load_target=database)
    check_target(mode, OverrideSet.none(), target)


def test_the_refusal_exits_refused() -> None:
    with pytest.raises(NonEmptyTargetError) as caught:
        check_target(RestoreMode.ALL, OverrideSet.none(), POPULATED)
    assert exit_code_for(caught.value) is ExitCode.REFUSED


def test_the_refusal_names_no_credential(
    credential_sentinel: CredentialSentinel,
) -> None:
    """A target read from the sentinel's real Site: the password never appears.

    ``TargetState`` has no credential field, so this pins the type as much as
    the message; the CLI's own refusal output is E003-S02-006's.
    """
    from l3io.wp.backup.adapters.database import site_descriptor_from_wp_config
    from l3io.wp.backup.adapters.site_files import LocalSiteFiles

    s = credential_sentinel
    descriptor, _ = site_descriptor_from_wp_config(
        s.site, config_file=s.config, environ={}, secrets=SecretRegistry()
    )
    files, root = LocalSiteFiles(), Path(descriptor.site_root)
    target = TargetState(
        site_root_empty=files.is_empty(root),
        database=FULL_DB,
        config_exists=files.file_exists(s.config),
        site_root=descriptor.site_root,
        site_root_entries=files.entry_count(root),
        database_location=descriptor.database_location(),
        load_target=None,
        load_location=None,
    )
    with pytest.raises(NonEmptyTargetError) as caught:
        check_target(RestoreMode.ALL, OverrideSet.none(), target)
    err = caught.value
    assert err.parts == ("files", "database")
    for text in (str(err), err.found, err.detail):
        assert s.password not in text


def test_build_restore_plan_refuses_before_planning() -> None:
    with pytest.raises(NonEmptyTargetError):
        plan(RestoreMode.ALL, overrides=OverrideSet.none())
    assert plan(RestoreMode.ALL, overrides=OverrideSet.none(), target=EMPTY).phases


def test_a_refused_run_makes_no_command_port_call(tmp_path: Path) -> None:
    """Recording adapters: the refusal comes from the plan, before the executor."""
    h = Harness(work=tmp_path, archive_path=write_v1(tmp_path))
    h.overrides = OverrideSet.none()
    with pytest.raises(NonEmptyTargetError):
        h.run()
    assert h.log.calls == []
    assert h.slot_calls == 0
    assert h.snapshots == []


def test_a_survey_without_its_location_is_named_by_role() -> None:
    target = dataclasses.replace(EMPTY, load_target=FULL_DB, load_location=None)
    with pytest.raises(RelocationTargetNotEmptyError) as caught:
        check_target(RestoreMode.DATABASE_CLEAN, OverrideSet.none(), target)
    assert caught.value.database == "the load database"


# --------------------------------------------------------------------------
# Owner decision 57: a Site Root that looks like WordPress needs no --force
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", [m for m in RestoreMode if restores_files(m)])
def test_a_wordpress_site_root_needs_no_waiver(mode: RestoreMode) -> None:
    wordpress = dataclasses.replace(TARGETS["files"], site_root_wordpress=True)
    check_target(mode, OverrideSet.none(), wordpress)


def test_a_wordpress_site_root_never_waives_the_database() -> None:
    """The files narrowing leaves the database part as it is (part 2's seam)."""
    wordpress = dataclasses.replace(POPULATED, site_root_wordpress=True)
    with pytest.raises(NonEmptyTargetError) as caught:
        check_target(RestoreMode.ALL, OverrideSet.none(), wordpress)
    assert caught.value.parts == ("database",)


@pytest.mark.parametrize(
    "database",
    [
        WORDPRESS_DB,
        dataclasses.replace(WORDPRESS_DB, prefixed_tables=None),
        dataclasses.replace(WORDPRESS_DB, is_wordpress=False),
        EMPTY_DB,
    ],
    ids=["wordpress", "unlisted-but-wordpress", "prefixed-tables-only", "empty"],
)
@pytest.mark.parametrize("mode", [m for m in RestoreMode if restores_database(m)])
def test_a_wordpress_or_empty_database_needs_no_waiver(
    mode: RestoreMode, database: DatabaseTargetState
) -> None:
    """Owner decision 57: the Safety Snapshot and the rollback cover it. (A
    relocation's load database is decision 73's: it must hold no table.)"""
    target = dataclasses.replace(EMPTY, database=database)
    check_target(mode, OverrideSet.none(), target)


def test_routines_without_a_prefixed_table_need_the_waiver() -> None:
    routines = dataclasses.replace(EMPTY_DB, routine_count=2)
    target = dataclasses.replace(EMPTY, database=routines)
    with pytest.raises(NonEmptyTargetError):
        check_target(RestoreMode.DATABASE, OverrideSet.none(), target)
