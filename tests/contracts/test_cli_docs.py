"""AG-25 / FR-21 — the README's CLI usage and exit-code table are generated.

Runs ``tools/gen_cli_docs.py --check`` (CI also runs it in the ``contracts``
job), and proves the check fires on planted drift in a copy of the README.
The scope of what must appear is derived from the ``ExitCode`` enum and the
parser's own subcommands, never a hand list.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import pytest

from l3io.wp.backup.cli.parser import build_parser
from l3io.wp.backup.domain.exit_codes import DESCRIPTIONS, ExitCode

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "gen_cli_docs.py"
README = ROOT / "README.md"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - a fixed in-repo script
        [sys.executable, str(TOOL), *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
    )


def test_the_readme_is_current() -> None:
    result = _run("--check")
    assert result.returncode == 0, result.stdout + result.stderr


def _subcommands() -> list[str]:
    parser = build_parser()
    return [
        name
        for action in parser._actions  # noqa: SLF001 - argparse has no public API
        if isinstance(action, argparse._SubParsersAction)  # noqa: SLF001
        for name in action.choices
    ]


@pytest.mark.parametrize("code", list(ExitCode), ids=lambda c: c.name)
def test_every_code_is_in_the_readme_and_the_help(code: ExitCode) -> None:
    readme = README.read_text(encoding="utf-8")
    assert f"| {int(code)} | `{code.name}` | {DESCRIPTIONS[code]} |" in readme
    assert code.name in build_parser().format_help()


def test_every_subcommand_is_in_the_readme() -> None:
    names = _subcommands()
    assert names, "the parser has no subcommands: nothing was checked"
    readme = README.read_text(encoding="utf-8")
    for name in names:
        assert f"$ wpbackup2 {name} --help" in readme


@pytest.mark.parametrize(
    "drift",
    [
        lambda t: t.replace("| `LOCKED` |", "| `LOCKD` |"),
        lambda t: t.replace("--wp-dir WP_DIR", "--wp-dir DIR", 1),
    ],
    ids=["exit-code-row", "usage-text"],
)
def test_check_fails_on_planted_drift(tmp_path: Path, drift: object) -> None:
    copy = tmp_path / "README.md"
    text = README.read_text(encoding="utf-8")
    planted = drift(text)  # type: ignore[operator]
    assert planted != text, "the planted drift changed nothing"
    copy.write_text(planted, encoding="utf-8")
    result = _run("--check", "--readme", str(copy))
    assert result.returncode == 1
    assert "README.md is stale" in result.stdout
    assert copy.read_text(encoding="utf-8") == planted, "--check wrote the file"


def test_a_missing_block_is_an_error(tmp_path: Path) -> None:
    copy = tmp_path / "README.md"
    copy.write_text("# no markers\n", encoding="utf-8")
    result = _run("--check", "--readme", str(copy))
    assert result.returncode != 0
    assert "no well-formed" in result.stderr
