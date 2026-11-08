#!/usr/bin/env python3
"""AG-25 — the README's CLI usage and exit-code table, generated, never hand-kept.

Writes two blocks into ``README.md``, each between its own marker comments:

* ``cli-usage`` — ``build_parser().format_help()`` for the top-level parser and
  for every subcommand the parser registers, nested ones included (found by
  walking the parser recursively, not listed here);
* ``exit-codes`` — one row per :class:`~l3io.wp.backup.domain.exit_codes.ExitCode`
  member, with its :data:`~l3io.wp.backup.domain.exit_codes.DESCRIPTIONS` line.

``--check`` writes nothing and exits 1 with a diff when the README differs from
what would be written; CI runs it in the ``contracts`` job and
``tests/contracts/test_cli_docs.py`` runs it under pytest.

Help text is rendered at a fixed width with colour disabled, so the output does
not depend on the terminal or the Python version's colour default.

Every BEGIN/END pair of a block is replaced, so a block duplicated by a
copy-paste or a merge is regenerated too, never left stale; a BEGIN without
its END (or an END without a BEGIN) fails.

**What this check does NOT cover:** prose elsewhere in the README that names a
flag or a code. Only the marked blocks are generated. The parser walk reads
argparse's private ``_actions``/``_SubParsersAction`` (no public API exists;
checked on CPython 3.11-3.14).
"""

from __future__ import annotations

import argparse
import difflib
import os
import sys
from pathlib import Path

README = Path(__file__).resolve().parent.parent / "README.md"
BLOCKS = ("cli-usage", "exit-codes")
_WIDTH = "100"


def _begin(name: str) -> str:
    return f"<!-- BEGIN GENERATED: {name} (tools/gen_cli_docs.py) -->"


def _end(name: str) -> str:
    return f"<!-- END GENERATED: {name} -->"


def _help(parser: argparse.ArgumentParser) -> str:
    return parser.format_help().rstrip("\n")


def _sections(parser: argparse.ArgumentParser, command: str) -> list[str]:
    """``parser``'s help, then each of its subcommands' help, recursively."""
    sections = [f"```text\n$ {command} --help\n{_help(parser)}\n```"]
    # argparse has no public way to enumerate subcommands.
    for action in parser._actions:  # noqa: SLF001
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            for name, sub in action.choices.items():
                sections.extend(_sections(sub, f"{command} {name}"))
    return sections


def _usage() -> str:
    from l3io.wp.backup.cli.parser import build_parser

    return "\n\n".join(_sections(build_parser(), "wpbackup2"))


def _exit_codes() -> str:
    from l3io.wp.backup.domain.exit_codes import DESCRIPTIONS, RESERVED, ExitCode

    rows = [
        f"| {int(code)} | `{code.name}` | {DESCRIPTIONS[code]} |" for code in ExitCode
    ]
    reserved = ", ".join(str(value) for value in sorted(RESERVED))
    return "\n".join(
        [
            "| Code | Name | Meaning |",
            "|---:|---|---|",
            *rows,
            "",
            f"Reserved for the `l3io-wp-*` family and never used: {reserved}.",
        ]
    )


def render(text: str) -> str:
    """``text`` with every generated block replaced by its current content."""
    content = {"cli-usage": _usage(), "exit-codes": _exit_codes()}
    for name in BLOCKS:
        begin, end = _begin(name), _end(name)
        if text.count(begin) != text.count(end) or begin not in text:
            msg = f"README.md has no well-formed {name!r} block ({begin} ... {end})"
            raise SystemExit(msg)
        out: list[str] = []
        rest = text
        while begin in rest:
            start = rest.find(begin)
            stop = rest.find(end, start)
            following = rest.find(begin, start + len(begin))
            if stop < 0 or 0 <= following < stop:
                msg = f"README.md has an unclosed {name!r} block ({begin})"
                raise SystemExit(msg)
            out.append(rest[: start + len(begin)] + f"\n{content[name]}\n" + end)
            rest = rest[stop + len(end) :]
        text = "".join(out) + rest
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail on drift")
    parser.add_argument("--readme", type=Path, default=README)
    args = parser.parse_args(argv)
    os.environ["COLUMNS"] = _WIDTH
    os.environ["NO_COLOR"] = "1"
    os.environ["PYTHON_COLORS"] = "0"
    os.environ.pop("FORCE_COLOR", None)
    current = args.readme.read_text(encoding="utf-8")
    wanted = render(current)
    if wanted == current:
        return 0
    if args.check:
        sys.stdout.writelines(
            difflib.unified_diff(
                current.splitlines(keepends=True),
                wanted.splitlines(keepends=True),
                "README.md",
                "README.md (generated)",
            )
        )
        print("README.md is stale: run `uv run python tools/gen_cli_docs.py`")
        return 1
    args.readme.write_text(wanted, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
