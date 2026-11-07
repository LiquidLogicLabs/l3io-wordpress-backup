"""FR-4 / NFR-3 — no passphrase or password is a command-line value.

A value on the command line is visible to every user through the process
list. Passphrases come only from AD-26's chain, so every action whose
``dest`` mentions ``passphrase`` must be a ``store_true`` switch. No action's
``dest`` may mention ``password`` either, except the two frozen v0.2.21 flags
AD-22 keeps (:data:`FROZEN_PASSWORD_FLAGS`), whose help states the exposure,
and a flag that names a **file** holding one (E008-S01-001, owner decision
2026-11-07): ``dest`` ends in ``_file`` and its argparse ``type`` is
``pathlib.Path``, so what reaches ``argv`` is a path, never the secret.

Scope is every action of every parser, walked from ``build_parser()``, so a
subcommand added later is covered the moment it exists.

**Not covered:** a secret under a ``dest`` that names neither word
(``--token``, ``--key``). Review is the guard for that.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from pathlib import Path

from l3io.wp.backup.cli.parser import RESTORE_COMMAND, build_parser

#: AD-22's named allowance: ``(subcommand, dest)``.
FROZEN_PASSWORD_FLAGS = frozenset(
    {(RESTORE_COMMAND, "admin_password"), (RESTORE_COMMAND, "new_db_password")}
)


def _actions(parser: argparse.ArgumentParser) -> Iterator[tuple[str, argparse.Action]]:
    for action in parser._actions:  # noqa: SLF001 - argparse has no public API
        yield "", action
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            for name, sub in action.choices.items():
                for inner in sub._actions:  # noqa: SLF001
                    yield name, inner


def _violations(parser: argparse.ArgumentParser) -> list[str]:
    found = []
    for command, action in _actions(parser):
        dest = action.dest.lower()
        if "passphrase" in dest and not isinstance(
            action,
            argparse._StoreTrueAction,  # noqa: SLF001
        ):
            found.append(f"{command} {action.dest}: a passphrase flag takes a value")
        path_valued = dest.endswith("_file") and action.type is Path
        if (
            "password" in dest
            and (command, action.dest) not in FROZEN_PASSWORD_FLAGS
            and not path_valued
        ):
            found.append(
                f"{command} {action.dest}: a password flag outside AD-22's allowance"
            )
    return found


def test_no_flag_takes_a_secret_value() -> None:
    assert _violations(build_parser()) == []


def test_each_allowed_password_flag_exists_and_warns() -> None:
    present = {(c, a.dest): a for c, a in _actions(build_parser())}
    for key in FROZEN_PASSWORD_FLAGS:
        assert key in present, f"{key} is allowed but not registered"
        assert "process list" in (present[key].help or "")
    admin = present[(RESTORE_COMMAND, "admin_password")]
    assert "L3IO_WP_BACKUP_ADMIN_PASSWORD" in (admin.help or "")


def test_the_walk_reaches_subcommands_and_catches_planted_values() -> None:
    planted = argparse.ArgumentParser()
    planted.add_argument("--passphrase")
    subs = planted.add_subparsers(dest="action")
    sub = subs.add_parser("later")
    sub.add_argument("--db-password")
    sub.add_argument("--encrypt-with-passphrase", action="store_true")
    assert _violations(planted) == [
        " passphrase: a passphrase flag takes a value",
        "later db_password: a password flag outside AD-22's allowance",
    ]


def test_only_a_path_valued_file_flag_is_allowed() -> None:
    planted = argparse.ArgumentParser()
    planted.add_argument("--x-password-file", type=Path)
    planted.add_argument("--y-password-file")
    planted.add_argument("--z-password", type=Path)
    assert _violations(planted) == [
        " y_password_file: a password flag outside AD-22's allowance",
        " z_password: a password flag outside AD-22's allowance",
    ]
