"""UX-01-11 / AD-5 — one help style across every parser the CLI builds.

The parsers are found by walking ``build_parser()`` recursively, never listed,
so a subcommand added later is held to the same style.
"""

from __future__ import annotations

import argparse

import pytest

from l3io.wp.backup.cli.parser import LOG_LEVELS, build_parser, exit_code_epilog


def _parsers(
    parser: argparse.ArgumentParser, name: str = "wpbackup2"
) -> list[tuple[str, argparse.ArgumentParser]]:
    found = [(name, parser)]
    for action in parser._actions:  # noqa: SLF001 - argparse has no public API
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            for sub_name, sub in action.choices.items():
                found.extend(_parsers(sub, f"{name} {sub_name}"))
    return found


PARSERS = _parsers(build_parser())


def help_style_violations(
    parsers: list[tuple[str, argparse.ArgumentParser]],
) -> list[str]:
    """Each help line that is not capitalised or ends with a period."""
    found: list[str] = []
    for name, parser in parsers:
        for action in parser._actions:  # noqa: SLF001
            text = action.help
            if text is None or text == argparse.SUPPRESS:
                continue
            flag = "/".join(action.option_strings) or action.dest
            if not text[:1].isupper():
                found.append(f"{name} {flag}: not capitalised")
            if text.rstrip().endswith("."):
                found.append(f"{name} {flag}: ends with a period")
    return found


def test_the_walk_finds_every_subcommand() -> None:
    names = [name for name, _ in PARSERS]
    assert len(names) > 1
    assert {"wpbackup2 backup", "wpbackup2 restore", "wpbackup2 verify"} <= set(names)


def test_every_help_line_is_capitalised_without_a_period() -> None:
    assert help_style_violations(PARSERS) == []


def test_the_style_check_sees_a_planted_violation() -> None:
    planted = argparse.ArgumentParser(add_help=False)
    planted.add_argument("--x", help="lower case.")
    assert help_style_violations([("planted", planted)]) == [
        "planted --x: not capitalised",
        "planted --x: ends with a period",
    ]


@pytest.mark.parametrize(("name", "parser"), PARSERS, ids=[n for n, _ in PARSERS])
def test_every_parser_shows_a_description_and_the_exit_codes(
    name: str, parser: argparse.ArgumentParser
) -> None:
    text = parser.format_help()
    assert parser.description, name
    assert exit_code_epilog() in text, name


def test_log_level_help_names_its_choices() -> None:
    text = build_parser().format_help()
    assert "--log-level {" + ",".join(LOG_LEVELS) + "}" in text


@pytest.mark.parametrize(("name", "parser"), PARSERS, ids=[n for n, _ in PARSERS])
def test_dry_run_help_says_the_site_lock_is_taken(
    name: str, parser: argparse.ArgumentParser
) -> None:
    """AD-5 (BL-E002-096): a Dry Run takes the Site Lock, so it never says
    it changes nothing where it can lock (verify takes no lock)."""
    flags = [a for a in parser._actions if "--whatIf" in a.option_strings]  # noqa: SLF001
    if name.endswith((" verify", " doctor")):
        return  # read-only commands: no Site Lock
    (what_if,) = flags
    assert what_if.help is not None
    assert "Site Lock" in what_if.help, name
    assert "change nothing" not in what_if.help, name
