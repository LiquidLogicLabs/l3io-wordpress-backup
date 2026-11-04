"""AD-22 — a feature that has not landed is accepted and explained, never ignored.

One case per entry of :data:`~l3io.wp.backup.cli.parser.NOT_YET_IMPLEMENTED`,
derived from the mapping itself: each entry must name a real subcommand or a
real option on that subcommand (an entry naming nothing would make its own
case vacuous), and using it must exit ``USAGE`` with the feature and the
delivering story in the message, before any Run opens.

The scope half: every option registered on the restore subcommand — derived
from the parser, not listed — is either listed here (so using it exits
``USAGE``) or read by the restore root, so a flag added to restore cannot slip
past as silently accepted. Since E003-S02-006 binds the subcommand, "read" is
checked by AST: an ``args.<dest>`` attribute in ``cli/restore.py`` or
``cli/__init__.py`` (the two modules that receive ``args`` for a restore).
"""

from __future__ import annotations

import argparse
import ast
from pathlib import Path

import pytest

import l3io.wp.backup.cli as cli_package
from l3io.wp.backup.cli import main
from l3io.wp.backup.cli import parser as cli_parser
from l3io.wp.backup.cli.parser import NOT_YET_IMPLEMENTED, RESTORE_COMMAND, build_parser
from l3io.wp.backup.domain.exit_codes import ExitCode


def _subparsers() -> dict[str, argparse.ArgumentParser]:
    for action in build_parser()._actions:  # noqa: SLF001 - argparse has no public API
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            return dict(action.choices)
    return {}


def _option(command: str, flag: str) -> argparse.Action:
    return _subparsers()[command]._option_string_actions[flag]  # noqa: SLF001


def _with_value(action: argparse.Action, flag: str) -> list[str]:
    return [flag] if action.nargs == 0 else [flag, "x"]


def _required(command: str) -> list[str]:
    argv: list[str] = []
    for action in _subparsers()[command]._actions:  # noqa: SLF001
        if action.required and action.option_strings:
            argv += _with_value(action, action.option_strings[0])
    return argv


def _argv(key: str) -> list[str]:
    command, _, flag = key.partition(" ")
    argv = [command, *_required(command)]
    if flag:
        argv += _with_value(_option(command, flag), flag)
        if flag.endswith("-aws-region"):
            # A region alone is a usage error of its own; give its partner.
            argv += [flag.replace("-aws-region", "-aws-secret-id"), "x"]
    return argv


def _invalid_entries(entries: dict[str, str]) -> list[str]:
    subparsers = _subparsers()
    invalid = []
    for key in entries:
        command, _, flag = key.partition(" ")
        if command not in subparsers:
            invalid.append(key)
            continue
        if not flag:
            continue
        actions = subparsers[command]._option_string_actions  # noqa: SLF001
        if flag not in actions or actions[flag].dest != cli_parser._dest(flag):  # noqa: SLF001
            invalid.append(key)
    return invalid


def _run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[object, str]:
    with pytest.raises(SystemExit) as raised:
        main(argv)
    return raised.value.code, capsys.readouterr().err


def test_every_entry_names_a_real_subcommand_or_option() -> None:
    assert _invalid_entries(dict(NOT_YET_IMPLEMENTED)) == []


def test_an_entry_naming_no_real_option_is_caught() -> None:
    planted = {
        "restore --no-such-flag": "x",
        "nosuchcommand": "x",
        "backup --exclude": "x",
    }
    assert _invalid_entries(planted) == ["restore --no-such-flag", "nosuchcommand"]


@pytest.mark.parametrize("key", sorted(NOT_YET_IMPLEMENTED))
def test_using_an_unbound_feature_exits_usage_naming_it_and_its_story(
    key: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code, err = _run(_argv(key), capsys)
    assert code == int(ExitCode.USAGE)
    # A region needs its secret id on the command line too; when both are
    # unbound, either may be the one named, and both name the same story.
    partner = key.replace("-aws-region", "-aws-secret-id")
    assert key in err or (partner in err and partner in NOT_YET_IMPLEMENTED)
    assert NOT_YET_IMPLEMENTED[key] in err
    assert "run_id" not in err


def test_only_appointed_in_asg_names_chesney_not_unrecognized(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, err = _run(_argv("restore --only-appointed-in-asg"), capsys)
    assert code == int(ExitCode.USAGE)
    assert "chesney" in err
    assert "unrecognized arguments" not in err


def _restore_options() -> list[str]:
    return sorted(
        action.option_strings[-1]
        for action in _subparsers()[RESTORE_COMMAND]._actions  # noqa: SLF001
        if action.option_strings and not isinstance(action, argparse._HelpAction)  # noqa: SLF001
    )


def test_the_restore_option_derivation_is_not_empty() -> None:
    assert len(_restore_options()) > 20


_ROOT_MODULES = ("restore.py", "__init__.py")


def _args_read(sources: list[str]) -> set[str]:
    """Every ``args.<name>`` attribute read in ``sources``."""
    names: set[str] = set()
    for source in sources:
        for node in ast.walk(ast.parse(source)):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "args"
            ):
                names.add(node.attr)
    return names


def _root_sources() -> list[str]:
    folder = Path(cli_package.__file__).parent
    return [(folder / name).read_text(encoding="utf-8") for name in _ROOT_MODULES]


def _silently_accepted(flags: list[str], read: set[str]) -> list[str]:
    """The flags neither listed as unbound nor read by the restore root."""
    actions = _subparsers()[RESTORE_COMMAND]._option_string_actions  # noqa: SLF001
    return [
        flag
        for flag in flags
        if f"{RESTORE_COMMAND} {flag}" not in NOT_YET_IMPLEMENTED
        and actions[flag].dest not in read
    ]


@pytest.mark.parametrize("flag", _restore_options())
def test_no_restore_option_is_silently_accepted(flag: str) -> None:
    assert _silently_accepted([flag], _args_read(_root_sources())) == []


def test_an_unread_restore_option_is_caught() -> None:
    """The scope attack: a registered flag the root never reads is reported."""
    read = _args_read(_root_sources()) - {"archive_database"}
    assert _silently_accepted(["--archive-database"], read) == ["--archive-database"]


def test_restore_without_an_archive_exits_usage(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, err = _run([RESTORE_COMMAND, "--wp-dir", "x"], capsys)
    assert code == int(ExitCode.USAGE)
    assert "--archive" in err
    assert "run_id" not in err


def test_a_rejected_combination_wins_over_the_unbound_subcommand(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The table's message names the combination that would express a Mode."""
    code, err = _run(
        [RESTORE_COMMAND, "--wp-dir", "x", "--mode", "db", "--cleanfirst"], capsys
    )
    assert code == int(ExitCode.USAGE)
    assert "--drop-database" in err
    assert "E003-S02-006" not in err
