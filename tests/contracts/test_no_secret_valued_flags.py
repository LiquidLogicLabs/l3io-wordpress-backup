"""FR-4 / NFR-3, as owner decision 60 amends them — a secret flag says so.

A value on the command line is visible to every user through the process
list. Owner decision 60 gives every setting a flag, the passphrase and the
database passwords included, so a flag may take a secret only when its
``dest`` is a secret setting (``cli/settings.SECRET_SETTINGS``, derived from
the ``SecretStr`` fields), and its help must say the value is visible in the
process list and name that setting's variable. Any other action whose
``dest`` mentions ``passphrase`` must be a ``store_true`` switch, and any other
``dest`` mentioning ``password`` must name a **file** holding one: ``dest``
ends in ``_file`` and its argparse ``type`` is ``pathlib.Path``, so what
reaches ``argv`` is a path, never the secret. A secret typed on the command
line logs WARNING ``password_on_command_line``.

Scope is every action of every parser, walked from ``build_parser()``, so a
subcommand added later is covered the moment it exists.

**Not covered:** a secret under a ``dest`` that names neither word
(``--token``, ``--key``) and is not a ``SecretStr`` setting. Review is the
guard for that.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from pathlib import Path

from l3io.wp.backup.cli.parser import build_parser
from l3io.wp.backup.cli.settings import SECRET_SETTINGS, env_name

#: The secret settings: the only ``dest`` a value-taking secret flag may have.
ALLOWED = SECRET_SETTINGS


def _actions(parser: argparse.ArgumentParser) -> Iterator[tuple[str, argparse.Action]]:
    for action in parser._actions:  # noqa: SLF001 - argparse has no public API
        yield "", action
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            for name, sub in action.choices.items():
                for inner in sub._actions:  # noqa: SLF001
                    yield name, inner


def _violations(
    parser: argparse.ArgumentParser, allowed: frozenset[str] = ALLOWED
) -> list[str]:
    found = []
    for command, action in _actions(parser):
        dest = action.dest.lower()
        path_valued = dest.endswith("_file") and action.type is Path
        if action.dest in allowed or path_valued:
            continue
        if "passphrase" in dest and not isinstance(
            action,
            argparse._StoreTrueAction,  # noqa: SLF001
        ):
            found.append(f"{command} {action.dest}: a passphrase flag takes a value")
        elif "password" in dest:
            found.append(
                f"{command} {action.dest}: a password flag outside the settings"
            )
    return found


def test_no_flag_takes_a_secret_value() -> None:
    assert _violations(build_parser()) == []


def test_each_secret_flag_exists_and_warns() -> None:
    present = [(c, a) for c, a in _actions(build_parser()) if a.dest in ALLOWED]
    assert {a.dest for _, a in present} == set(ALLOWED)
    for _, action in present:
        assert "process list" in (action.help or "")
        assert env_name(action.dest) in (action.help or "")
    assert {"passphrase", "db_password", "admin_password", "new_db_password"} == ALLOWED


def test_the_walk_reaches_subcommands_and_catches_planted_values() -> None:
    planted = argparse.ArgumentParser()
    planted.add_argument("--passphrase")
    subs = planted.add_subparsers(dest="action")
    sub = subs.add_parser("later")
    sub.add_argument("--db-password")
    sub.add_argument("--encrypt-with-passphrase", action="store_true")
    assert _violations(planted, frozenset()) == [
        " passphrase: a passphrase flag takes a value",
        "later db_password: a password flag outside the settings",
    ]


def test_only_a_path_valued_file_flag_is_allowed() -> None:
    planted = argparse.ArgumentParser()
    planted.add_argument("--x-password-file", type=Path)
    planted.add_argument("--y-password-file")
    planted.add_argument("--z-password", type=Path)
    assert _violations(planted) == [
        " y_password_file: a password flag outside the settings",
        " z_password: a password flag outside the settings",
    ]
