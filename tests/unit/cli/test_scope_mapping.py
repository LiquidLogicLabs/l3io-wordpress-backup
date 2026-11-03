"""FR-2/AD-22 — ``--mode`` maps onto Backup Scope through one total table.

The scope of this test is **derived from the parser**: the backup
subparser's ``--mode`` action and its ``choices``. A choice added to the
parser without an entry in ``CLI_MODE_TO_SCOPE`` fails here, and so does a
``BackupScope`` member no CLI value reaches.
"""

from __future__ import annotations

import argparse

import pytest

from l3io.wp.backup.cli.parser import BACKUP_COMMAND, build_parser
from l3io.wp.backup.cli.scope import CLI_MODE_TO_SCOPE, backup_scope_from_cli
from l3io.wp.backup.domain.backup_scope import BackupScope

# argparse exposes no public introspection API; the private access stays here,
# as in tests/contracts/test_flag_map.py.


def _backup_subparser(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    for action in parser._actions:  # noqa: SLF001
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            subparser = action.choices[BACKUP_COMMAND]
            assert isinstance(subparser, argparse.ArgumentParser)
            return subparser
    msg = "the CLI parser registers no subcommands at all"
    raise AssertionError(msg)


def _mode_action(parser: argparse.ArgumentParser) -> argparse.Action:
    actions = [a for a in parser._actions if "--mode" in a.option_strings]  # noqa: SLF001
    assert len(actions) == 1, f"expected one --mode action, found {len(actions)}"
    return actions[0]


def _mode_choices(parser: argparse.ArgumentParser) -> set[str]:
    choices = _mode_action(parser).choices
    assert choices is not None, "--mode is not a closed choices set"
    return {str(choice) for choice in choices}


def test_the_mapping_covers_exactly_the_parsers_choices() -> None:
    assert set(CLI_MODE_TO_SCOPE) == _mode_choices(_backup_subparser(build_parser()))


def test_the_mapping_is_a_bijection_onto_backup_scope() -> None:
    values = list(CLI_MODE_TO_SCOPE.values())
    assert len(values) == len(set(values))
    assert set(values) == set(BackupScope)


def test_the_cli_spelling_is_frozen_and_defaults_to_all() -> None:
    action = _mode_action(_backup_subparser(build_parser()))
    assert list(action.choices or ()) == ["files", "db", "all"]
    assert action.default == "all"
    assert backup_scope_from_cli(action.default) is BackupScope.ALL


def test_db_is_the_database_scope() -> None:
    assert backup_scope_from_cli("db") is BackupScope.DATABASE


@pytest.mark.parametrize("value", ["database", "DB", "Files", "", "ALL"])
def test_an_unmapped_value_has_no_fallback(value: str) -> None:
    with pytest.raises(KeyError):
        backup_scope_from_cli(value)


@pytest.mark.parametrize("value", ["database", "DB", "Files"])
def test_the_parser_refuses_a_value_outside_the_choices(
    value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exited:
        build_parser().parse_args(["backup", "--wp-dir", "/x", "--mode", value])
    assert exited.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_the_mapping_is_immutable() -> None:
    with pytest.raises(TypeError):
        CLI_MODE_TO_SCOPE["everything"] = BackupScope.ALL  # type: ignore[index]


def test_the_derivation_sees_a_planted_choice() -> None:
    """The scope half of the guard: a choice added to ``--mode`` is seen."""
    planted = argparse.ArgumentParser()
    planted.add_argument("--mode", choices=["files", "db", "all", "planted"])
    derived = _mode_choices(planted)
    assert derived == {"files", "db", "all", "planted"}
    assert set(CLI_MODE_TO_SCOPE) != derived
