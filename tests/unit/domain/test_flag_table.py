"""AD-25 — the rows of the restore flag table, checked against FR-6's meanings.

``tests/contracts/test_flag_map.py`` proves the table covers exactly the
parser's combinations; this module proves what the rows *say*: ``--force``
selects no Mode, destruction of each part needs its own flag, and every
rejection names the flags that would express a real Mode.
"""

from __future__ import annotations

import ast
import inspect
import re

import pytest

from l3io.wp.backup.cli.scope import CLI_MODE_TO_SCOPE
from l3io.wp.backup.domain import flag_map
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.errors import FlagCombinationRejectedError, UsageError
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
    OverrideSet,
    RestoreMode,
    RestorePart,
    clears_site_root,
    removes_database,
    restores_database,
    restores_files,
)

PARTS: tuple[RestorePart, ...] = ("files", "database")


def _flags(key: FlagCombination) -> dict[str, object]:
    return dict(key)


def _with(key: FlagCombination, **changes: object) -> FlagCombination:
    values = _flags(key) | changes
    return tuple(sorted(values.items()))


def _restores(mode: RestoreMode, part: RestorePart) -> bool:
    return restores_files(mode) if part == "files" else restores_database(mode)


def _resolved() -> dict[FlagCombination, tuple[RestoreMode, OverrideSet]]:
    return {k: v for k, v in FLAG_MAP.items() if not isinstance(v, FlagRejection)}


def test_the_table_has_all_24_combinations() -> None:
    assert len(FLAG_MAP) == 24


@pytest.mark.parametrize("mode", list(RestoreMode), ids=str)
def test_every_mode_is_reached_by_exactly_one_unforced_combination(
    mode: RestoreMode,
) -> None:
    reaching = [
        key
        for key, (entry_mode, _) in _resolved().items()
        if entry_mode is mode and not _flags(key)["force"]
    ]
    assert len(reaching) == 1, reaching


def test_all_clean_needs_both_cleanfirst_and_drop_database() -> None:
    for key, (mode, _) in _resolved().items():
        if mode is RestoreMode.ALL_CLEAN:
            assert _flags(key)["cleanfirst"] is True
            assert _flags(key)["drop_database"] is True


@pytest.mark.parametrize("flag", ["cleanfirst", "drop_database", "force"])
def test_no_single_flag_makes_a_mode_destroy_both_parts(flag: str) -> None:
    """Turning one flag on never turns a Mode into clear-and-drop."""
    for key, (mode, _) in _resolved().items():
        if _flags(key)[flag] or (clears_site_root(mode) and removes_database(mode)):
            continue
        after = FLAG_MAP[_with(key, **{flag: True})]
        if isinstance(after, FlagRejection):
            continue
        assert not (clears_site_root(after[0]) and removes_database(after[0])), key


def test_force_never_changes_a_rows_mode() -> None:
    for key, entry in FLAG_MAP.items():
        if _flags(key)["force"]:
            continue
        forced = FLAG_MAP[_with(key, force=True)]
        if isinstance(entry, FlagRejection):
            assert forced == entry
        else:
            assert not isinstance(forced, FlagRejection)
            assert forced[0] is entry[0]
            assert entry[1] == OverrideSet.none()


@pytest.mark.parametrize("part", PARTS)
def test_force_waives_exactly_the_parts_the_mode_restores(part: RestorePart) -> None:
    for key, (mode, overrides) in _resolved().items():
        expected = bool(_flags(key)["force"]) and _restores(mode, part)
        assert overrides.waives(part) is expected, key


def test_every_rejection_names_a_flag() -> None:
    rejections = [v for v in FLAG_MAP.values() if isinstance(v, FlagRejection)]
    assert rejections
    for rejection in rejections:
        assert re.search(r"--[a-z]", rejection.reason), rejection
        assert re.search(r"--[a-z]", rejection.use_instead), rejection


def test_the_mode_axis_agrees_with_the_cli_scope_translation() -> None:
    """``--mode`` means the same parts for restore as ``CLI_MODE_TO_SCOPE`` does."""
    for scope in CLI_MODE_TO_SCOPE.values():
        key = combination(
            mode=scope, cleanfirst=False, drop_database=False, force=False
        )
        entry = FLAG_MAP[key]
        assert not isinstance(entry, FlagRejection)
        mode = entry[0]
        assert restores_files(mode) is (scope in {BackupScope.FILES, BackupScope.ALL})
        assert restores_database(mode) is (
            scope in {BackupScope.DATABASE, BackupScope.ALL}
        )


def test_resolve_flags_returns_the_rows_mode() -> None:
    key = combination(
        mode=BackupScope.DATABASE, cleanfirst=False, drop_database=True, force=False
    )
    assert resolve_flags(key) == (RestoreMode.DATABASE_CLEAN, OverrideSet.none())


def test_a_rejection_raises_a_usage_error_with_the_tables_text() -> None:
    key = combination(
        mode=BackupScope.DATABASE, cleanfirst=True, drop_database=False, force=False
    )
    entry = FLAG_MAP[key]
    assert isinstance(entry, FlagRejection)
    with pytest.raises(FlagCombinationRejectedError) as raised:
        resolve_flags(key)
    assert isinstance(raised.value, UsageError)
    assert raised.value.exit_code is ExitCode.USAGE
    assert entry.reason in str(raised.value)
    assert "--drop-database" in str(raised.value)


def test_a_key_outside_the_table_has_no_default() -> None:
    with pytest.raises(KeyError):
        resolve_flags((("mode", "everything"),))


@pytest.mark.parametrize("part", PARTS)
@pytest.mark.parametrize("mode", list(RestoreMode), ids=str)
def test_authorizing_combination_is_the_tables_forced_row(
    mode: RestoreMode, part: RestorePart
) -> None:
    if not _restores(mode, part):
        with pytest.raises(ValueError, match="does not restore"):
            authorizing_combination(mode, part)
        return
    key = authorizing_combination(mode, part)
    assert _flags(key)["force"] is True
    resolved_mode, overrides = resolve_flags(key)
    assert resolved_mode is mode
    assert overrides.waives(part)


def test_the_table_is_a_literal_not_a_comprehension() -> None:
    """Each row stays reviewable against FR-6: 24 written-out entries."""
    tree = ast.parse(inspect.getsource(flag_map))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "FLAG_MAP"
        ):
            assert isinstance(node.value, ast.Call)
            (table,) = node.value.args
            assert isinstance(table, ast.Dict)
            assert len(table.keys) == 24
            return
    pytest.fail("FLAG_MAP is not an annotated assignment in domain/flag_map.py")
