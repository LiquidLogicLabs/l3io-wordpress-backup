"""AD-25 — the CLI flag matrix maps to Restore Mode through one total function.

``--mode`` x ``--cleanfirst`` x ``--drop-database`` x ``--force`` is 24
reachable states against six Restore Modes. The rule is that every reachable
combination has an entry in one table in ``domain/`` and there is no default
branch.

The point of this test is where its scope comes from. It is derived from the
**argparse parser's own actions** in the restore subparser's ``restore mode``
argument group — its ``store_true`` actions and its actions carrying
``choices`` — so a flag added to that group without an entry in the table
fails here, rather than falling into an implicit default that nobody chose. A
flag is classified where it is defined: a ``store_true`` or ``choices``
action in *neither* group fails too, so a mode-selecting flag cannot dodge the
table by being left out of the group.

``--mode`` values are the CLI's spellings; the table is keyed by the Backup
Scope ``CLI_MODE_TO_SCOPE`` translates them to, and the derivation applies
that same translation — there is no second spelling table.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from itertools import product

import pytest

from l3io.wp.backup.cli.parser import (
    RESTORE_COMMAND,
    RESTORE_MODE_GROUP,
    RESTORE_OPTIONS_GROUP,
    build_parser,
)
from l3io.wp.backup.cli.scope import CLI_MODE_TO_SCOPE
from l3io.wp.backup.domain import flag_map

#: How a parser value becomes a table value, per ``dest``: the CLI's own
#: translation, imported, not restated.
TRANSLATIONS: dict[str, Mapping[str, object]] = {"mode": CLI_MODE_TO_SCOPE}

# argparse exposes no public introspection API. `_actions`, `_SubParsersAction`
# and `_StoreTrueAction` are stable across every supported version and are used
# here rather than in the package itself, so the private access stays in the
# test that needs it.


def _restore_subparser(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    for action in parser._actions:  # noqa: SLF001
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            assert RESTORE_COMMAND in action.choices, (
                f"AD-22 freezes both subcommands; {RESTORE_COMMAND!r} is missing "
                f"from {sorted(action.choices)}"
            )
            subparser = action.choices[RESTORE_COMMAND]
            assert isinstance(subparser, argparse.ArgumentParser)
            return subparser
    msg = "the CLI parser registers no subcommands at all"
    raise AssertionError(msg)


def _is_classified_kind(action: argparse.Action) -> bool:
    """A switch or a ``choices`` action — the shapes that can select a Mode.

    A switch is any option taking no value (``store_true``, ``store_false``,
    ``store_const``, ``count``, ``BooleanOptionalAction``...), not only
    ``store_true``, so a mode flag of another shape cannot dodge the guard.
    """
    if isinstance(action, argparse._HelpAction | argparse._VersionAction):  # noqa: SLF001
        return False
    if not action.option_strings:
        return bool(action.choices)
    return action.nargs == 0 or bool(action.choices)


def _group(parser: argparse.ArgumentParser, title: str) -> list[argparse.Action]:
    for group in parser._action_groups:  # noqa: SLF001
        if group.title == title:
            return list(group._group_actions)  # noqa: SLF001
    msg = f"the restore subparser has no {title!r} argument group"
    raise AssertionError(msg)


def _mode_selecting_dimensions(
    parser: argparse.ArgumentParser,
) -> dict[str, tuple[object, ...]]:
    """Derive the mode-selecting flags, and their values, from ``parser``."""
    dimensions: dict[str, tuple[object, ...]] = {}
    for action in _group(parser, RESTORE_MODE_GROUP):
        if not _is_classified_kind(action):
            continue
        if isinstance(action, argparse._StoreTrueAction):  # noqa: SLF001
            dimensions[action.dest] = (False, True)
        elif not action.choices:
            # A switch of another shape: the table has no way to key it, so
            # it surfaces as an unmapped dimension rather than being skipped.
            dimensions[action.dest] = (f"<unsupported {type(action).__name__}>",)
        else:
            assert action.choices is not None
            translate = TRANSLATIONS.get(action.dest)
            dimensions[action.dest] = tuple(
                choice if translate is None else translate[choice]
                for choice in action.choices
            )
    return dimensions


def _unclassified(parser: argparse.ArgumentParser) -> list[str]:
    """``store_true``/``choices`` actions in neither restore group."""
    grouped = {
        id(action)
        for title in (RESTORE_MODE_GROUP, RESTORE_OPTIONS_GROUP)
        for action in _group(parser, title)
    }
    return [
        action.dest
        for action in parser._actions  # noqa: SLF001
        if _is_classified_kind(action) and id(action) not in grouped
    ]


def _reachable_combinations(
    dimensions: dict[str, tuple[object, ...]],
) -> set[flag_map.FlagCombination]:
    if not dimensions:
        # No mode-selecting flag exists, so there is no combination to map.
        # This branch is only reachable while the parser declares none, and
        # `test_flag_map_covers_exactly_the_parsers_mode_selecting_flags`
        # fails the moment that stops being true.
        return set()
    names = sorted(dimensions)
    return {
        tuple(sorted(zip(names, values, strict=True)))
        for values in product(*(dimensions[name] for name in names))
    }


def test_flag_map_covers_exactly_the_parsers_mode_selecting_flags() -> None:
    derived = set(_mode_selecting_dimensions(_restore_subparser(build_parser())))
    declared = set(flag_map.COVERED_FLAGS)

    assert derived == declared, (
        "AD-25 — the flag map's scope has drifted from the parser.\n"
        f"  in the parser but unmapped: {sorted(derived - declared)}\n"
        f"  mapped but not in the parser: {sorted(declared - derived)}\n"
        "Add the flag to l3io.wp.backup.domain.flag_map.COVERED_FLAGS and give "
        "every combination it creates an entry in FLAG_MAP."
    )


def test_flag_map_is_total_over_every_reachable_combination() -> None:
    dimensions = _mode_selecting_dimensions(_restore_subparser(build_parser()))
    expected = _reachable_combinations(dimensions)
    actual = set(flag_map.FLAG_MAP)

    assert actual == expected, (
        "AD-25 — the flag map is not total. There is no default branch, so "
        "every reachable combination needs an entry.\n"
        f"  unmapped combinations: {sorted(expected - actual)}\n"
        f"  entries for unreachable combinations: {sorted(actual - expected)}"
    )


def test_every_restore_flag_is_classified_where_it_is_defined() -> None:
    assert _unclassified(_restore_subparser(build_parser())) == []


def _planted_restore() -> argparse.ArgumentParser:
    """A throwaway restore subparser shaped like the real one."""
    planted = argparse.ArgumentParser()
    mode = planted.add_argument_group(RESTORE_MODE_GROUP)
    mode.add_argument("--mode", choices=["files", "db", "all"])
    mode.add_argument("--cleanfirst", action="store_true")
    mode.add_argument("--drop-database", action="store_true")
    mode.add_argument("--force", action="store_true")
    planted.add_argument_group(RESTORE_OPTIONS_GROUP).add_argument(
        "--no-safety-snapshot", action="store_true"
    )
    return planted


def test_the_derivation_sees_the_real_tables_shape() -> None:
    """The scope half: the planted twin derives exactly the table's keys."""
    dimensions = _mode_selecting_dimensions(_planted_restore())
    assert set(dimensions) == set(flag_map.COVERED_FLAGS)
    assert _reachable_combinations(dimensions) == set(flag_map.FLAG_MAP)


def test_a_flag_planted_in_the_restore_mode_group_without_an_entry_fails() -> None:
    planted = _planted_restore()
    for group in planted._action_groups:  # noqa: SLF001
        if group.title == RESTORE_MODE_GROUP:
            group.add_argument("--wipe-everything", action="store_true")
    dimensions = _mode_selecting_dimensions(planted)
    assert set(dimensions) - set(flag_map.COVERED_FLAGS) == {"wipe_everything"}
    assert _reachable_combinations(dimensions) != set(flag_map.FLAG_MAP)


SWITCH_SHAPES: list[dict[str, object]] = [
    {"action": "store_true"},
    {"action": "store_false"},
    {"action": "store_const", "const": 1},
    {"action": "count"},
    {"action": argparse.BooleanOptionalAction},
    {"choices": ["a", "b"]},
]
SWITCH_IDS = [
    "store_true",
    "store_false",
    "store_const",
    "count",
    "bool-opt",
    "choices",
]


@pytest.mark.parametrize("kwargs", SWITCH_SHAPES, ids=SWITCH_IDS)
def test_a_switch_of_any_shape_in_the_restore_mode_group_is_seen(
    kwargs: dict[str, object],
) -> None:
    planted = _planted_restore()
    for group in planted._action_groups:  # noqa: SLF001
        if group.title == RESTORE_MODE_GROUP:
            group.add_argument("--stray", **kwargs)  # type: ignore[arg-type]
    assert "stray" in _mode_selecting_dimensions(planted)


@pytest.mark.parametrize("kwargs", SWITCH_SHAPES, ids=SWITCH_IDS)
def test_a_flag_planted_outside_both_groups_fails(kwargs: dict[str, object]) -> None:
    planted = _planted_restore()
    planted.add_argument("--stray", **kwargs)  # type: ignore[arg-type]
    assert _unclassified(planted) == ["stray"]
