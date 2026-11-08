"""AD-22 / AG-10 — the frozen v0.2.21 CLI surface, checked against the legacy parser.

The expected spellings are **not** a hand list. They are read by ``ast`` from
the legacy parser, ``legacy/wpbackup2/__main__.py``, which stays on disk for exactly
this purpose: every ``add_argument`` option string, attributed to the
subcommand(s) it lived on (``parents=[...]`` followed). Should that file be
deleted, a committed snapshot (:data:`SNAPSHOT`) must replace it, or this
module fails rather than checking nothing.

Additions AD-22 permits are asserted where the story placed them. Every
action on every parser carries help text.

**Not covered:** behaviour behind a spelling — that a frozen flag *does* what
it did is each binding story's test; this module proves only that it parses.
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from collections.abc import Iterator
from importlib.metadata import entry_points
from pathlib import Path
from typing import NoReturn, get_type_hints

import pytest

import l3io.wp.backup.cli as cli_root
from l3io.wp.backup import __version__
from l3io.wp.backup.cli.parser import (
    BACKUP_COMMAND,
    DOCTOR_COMMAND,
    RESTORE_COMMAND,
    VERIFY_COMMAND,
    build_parser,
)

ROOT = Path(__file__).resolve().parents[2]
LEGACY = ROOT / "legacy" / "wpbackup2" / "__main__.py"
#: ``[[command, spelling], ...]``; ``""`` is the top-level parser.
SNAPSHOT = Path(__file__).with_name("legacy_cli_surface.json")
TOP = ""
FROZEN_COUNT = 19


def _call_name(node: ast.expr) -> tuple[str, str] | None:
    """``(receiver, method)`` for ``receiver.method(...)``."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
    ):
        return node.func.value.id, node.func.attr
    return None


def _legacy_surface_from_ast(source: str) -> set[tuple[str, str]]:
    tree = ast.parse(source)
    commands: dict[str, set[str]] = {}
    root: str | None = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        call = _call_name(node.value)
        if not isinstance(target, ast.Name) or call is None:
            continue
        if call[1] == "add_subparsers":
            root = call[0]
        if call[1] == "add_parser":
            assert isinstance(node.value, ast.Call)
            name = node.value.args[0]
            assert isinstance(name, ast.Constant)
            assert isinstance(name.value, str)
            commands.setdefault(target.id, set()).add(name.value)
            for keyword in node.value.keywords:
                if keyword.arg == "parents" and isinstance(keyword.value, ast.List):
                    for parent in keyword.value.elts:
                        assert isinstance(parent, ast.Name)
                        commands.setdefault(parent.id, set()).add(name.value)
    assert root is not None, "the legacy parser calls add_subparsers on no parser"
    commands[root] = {TOP}
    surface: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        call = _call_name(node) if isinstance(node, ast.expr) else None
        if call is None or call[1] != "add_argument":
            continue
        assert isinstance(node, ast.Call)
        spellings = [
            arg.value
            for arg in node.args
            if isinstance(arg, ast.Constant)
            and isinstance(arg.value, str)
            and arg.value.startswith("--")
        ]
        for command in commands.get(call[0], set()):
            surface.update((command, spelling) for spelling in spellings)
    return surface


def _legacy_surface() -> set[tuple[str, str]]:
    if LEGACY.is_file():
        return _legacy_surface_from_ast(LEGACY.read_text(encoding="utf-8"))
    if SNAPSHOT.is_file():
        return {tuple(row) for row in json.loads(SNAPSHOT.read_text("utf-8"))}  # type: ignore[misc]
    pytest.fail(
        f"{LEGACY} is gone and no snapshot replaces it at "
        f"{SNAPSHOT}: the frozen surface would be unchecked"
    )


def _subparsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:  # noqa: SLF001 - argparse has no public API
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            return dict(action.choices)
    return {}


def _parser_for(command: str) -> argparse.ArgumentParser:
    parser = build_parser()
    return parser if command == TOP else _subparsers(parser)[command]


def _spellings(command: str) -> set[str]:
    return set(_parser_for(command)._option_string_actions)  # noqa: SLF001


def test_the_legacy_surface_is_the_nineteen_frozen_spellings() -> None:
    """The derivation is not vacuous: it finds AD-22's nineteen spellings."""
    surface = _legacy_surface()
    assert len({spelling for _, spelling in surface}) == FROZEN_COUNT
    assert (TOP, "--whatIf") in surface
    assert (RESTORE_COMMAND, "--force") in surface
    assert (BACKUP_COMMAND, "--wp-dir") in surface
    assert (RESTORE_COMMAND, "--wp-dir") in surface


#: Collected when neither source exists, so the case fails instead of
#: collecting nothing (an empty parametrisation is a skip, not a failure).
_NO_SOURCE = ("<no legacy parser>", "<no snapshot>")


def _frozen_cases() -> list[tuple[str, str]]:
    """The frozen spellings, from whichever source exists, for collection."""
    if LEGACY.is_file():
        return sorted(_legacy_surface_from_ast(LEGACY.read_text("utf-8")))
    if SNAPSHOT.is_file():
        return sorted(
            (str(command), str(spelling))
            for command, spelling in json.loads(SNAPSHOT.read_text("utf-8"))
        )
    return [_NO_SOURCE]


@pytest.mark.parametrize(("command", "spelling"), _frozen_cases())
def test_each_frozen_spelling_lives_on_its_subcommand(
    command: str, spelling: str
) -> None:
    assert (command, spelling) != _NO_SOURCE, "no source of the frozen surface"
    assert spelling in _spellings(command)


def test_the_collected_cases_survive_the_legacy_parsers_removal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Deleting the legacy parser yields a failing case, never zero cases."""
    module = sys.modules[__name__]
    monkeypatch.setattr(module, "LEGACY", tmp_path / "gone.py")
    monkeypatch.setattr(module, "SNAPSHOT", tmp_path / "gone.json")
    assert _frozen_cases() == [_NO_SOURCE]
    snapshot = tmp_path / "snap.json"
    snapshot.write_text(json.dumps([["restore", "--force"]]), encoding="utf-8")
    monkeypatch.setattr(module, "SNAPSHOT", snapshot)
    assert _frozen_cases() == [("restore", "--force")]


def test_a_missing_legacy_parser_without_a_snapshot_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The scope half: deleting the source of truth cannot pass silently."""
    monkeypatch.setattr(sys.modules[__name__], "LEGACY", tmp_path / "gone.py")
    monkeypatch.setattr(sys.modules[__name__], "SNAPSHOT", tmp_path / "gone.json")
    with pytest.raises(pytest.fail.Exception, match="no snapshot replaces it"):
        _legacy_surface()


def test_the_ast_reader_follows_parents_onto_each_subcommand() -> None:
    planted = (
        "import argparse\n"
        "top = argparse.ArgumentParser()\n"
        "top.add_argument('--loud')\n"
        "subs = top.add_subparsers(dest='action')\n"
        "shared = argparse.ArgumentParser(add_help=False)\n"
        "shared.add_argument('--both')\n"
        "one = subs.add_parser('one', parents=[shared])\n"
        "two = subs.add_parser('two', parents=[shared])\n"
        "two.add_argument('--only-two', action='store_true')\n"
    )
    assert _legacy_surface_from_ast(planted) == {
        (TOP, "--loud"),
        ("one", "--both"),
        ("two", "--both"),
        ("two", "--only-two"),
    }


ADDITIONS = [
    (BACKUP_COMMAND, "--wp-config"),
    (RESTORE_COMMAND, "--wp-config"),
    (BACKUP_COMMAND, "--db-credentials-aws-secret-id"),
    (BACKUP_COMMAND, "--db-credentials-aws-region"),
    (RESTORE_COMMAND, "--db-credentials-aws-secret-id"),
    (RESTORE_COMMAND, "--db-credentials-aws-region"),
    (BACKUP_COMMAND, "--encrypt-with-passphrase"),
    (BACKUP_COMMAND, "--exclude"),
    (BACKUP_COMMAND, "--no-default-excludes"),
    (BACKUP_COMMAND, "--list-default-excludes"),
    (RESTORE_COMMAND, "--drop-database"),
    (RESTORE_COMMAND, "--no-safety-snapshot"),
    (RESTORE_COMMAND, "--snapshot-dir"),
    (RESTORE_COMMAND, "--resolve-interrupted-run"),
    (RESTORE_COMMAND, "--overwrite-wp-config"),
    (RESTORE_COMMAND, "--reset-existing-db-user"),
    (RESTORE_COMMAND, "--archive-database"),
    (RESTORE_COMMAND, "--admin-tls"),
    (RESTORE_COMMAND, "--admin-tls-ca"),
    # E008-S01-001 (owner decisions 2026-11-07).
    (RESTORE_COMMAND, "--new-db-password-file"),
    (RESTORE_COMMAND, "--allow-unencrypted"),
    (DOCTOR_COMMAND, "--wp-dir"),
    (DOCTOR_COMMAND, "--archive"),
    (DOCTOR_COMMAND, "--json"),
    # Close-out owner decisions 2026-11-07: BL-E002-098, -063, -064.
    (RESTORE_COMMAND, "--allow-root"),
    (BACKUP_COMMAND, "--confirm-db-host"),
    (BACKUP_COMMAND, "--allow-target-in-site-root"),
]


@pytest.mark.parametrize(("command", "spelling"), ADDITIONS)
def test_each_addition_is_where_the_story_placed_it(
    command: str, spelling: str
) -> None:
    assert spelling in _spellings(command)


def test_doctor_has_exactly_its_three_options() -> None:
    assert _spellings(DOCTOR_COMMAND) - {"-h", "--help"} == {
        "--wp-dir",
        "--archive",
        "--json",
    }


def test_the_subcommands_exist() -> None:
    assert {BACKUP_COMMAND, RESTORE_COMMAND, VERIFY_COMMAND, DOCTOR_COMMAND} <= set(
        _subparsers(build_parser())
    )


def _every_action() -> Iterator[tuple[str, argparse.Action]]:
    parser = build_parser()
    for action in parser._actions:  # noqa: SLF001
        yield TOP, action
    for name, sub in _subparsers(parser).items():
        for action in sub._actions:  # noqa: SLF001
            yield name, action


def test_every_flag_has_help_text() -> None:
    missing = [
        f"{command} {action.option_strings}"
        for command, action in _every_action()
        if action.option_strings and not action.help
    ]
    assert missing == []


def test_the_console_script_reaches_main(monkeypatch: pytest.MonkeyPatch) -> None:
    (script,) = entry_points(group="console_scripts", name="wpbackup2")
    calls: list[None] = []

    def fake_main() -> int:
        calls.append(None)
        return 0

    monkeypatch.setattr(cli_root, "main", fake_main)
    with pytest.raises(SystemExit) as raised:
        script.load()()
    assert raised.value.code == 0
    assert calls == [None]


def test_python_dash_m_wpbackup2_reaches_this_parser() -> None:
    """Run from the repository root, where a ``wpbackup2/`` tree would shadow it."""
    result = subprocess.run(
        [sys.executable, "-m", "wpbackup2", "--version"],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"wpbackup2 {__version__}"


def test_the_entry_never_returns_and_python_dash_m_needs_no_ignore() -> None:
    """CR-1 (BL-E003-121): ``run_from_cli`` is typed ``NoReturn``; ``__main__``
    calls it plainly, with no ``type: ignore`` (mypy --strict checks ``src``)."""
    assert get_type_hints(cli_root.run_from_cli)["return"] is NoReturn
    main_module = ROOT / "src" / "wpbackup2" / "__main__.py"
    assert "type: ignore" not in main_module.read_text(encoding="utf-8")


def test_python_dash_m_wpbackup2_exits_with_the_runs_code() -> None:
    """The plain call still exits with the Run's code: a usage error is 2."""
    result = subprocess.run(
        [sys.executable, "-m", "wpbackup2", "no-such-command"],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
        timeout=60,
    )
    assert result.returncode == 2, result.stderr
