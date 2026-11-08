"""UX-S02-02, UX-S02-05, ADV-S02-10 — ``docs/restore.md`` agrees with the code.

Each claim checked here is derived from its source of truth, never retyped:

- the Modes table's Flags column is ``FLAG_MAP``'s combination without
  ``--force`` for each Mode, rendered by ``cli/restore_request.py::render_flags``, and
  every ``RestoreMode`` has a row; only the table under the ``## Restore
  Modes`` heading is read, so another table's rows cannot stand in for it;
- every ``$ wpbackup2 ...`` command in the document parses with the real
  parser, so a documented spelling (``restore --whatIf``) cannot drift from
  what the CLI accepts;
- the staging database's name is described with ``core/restore.py``'s
  infix and length rule.

**Not covered:** the prose around them, and the worked examples' output text
(it was captured from a real Run; only the commands are re-checked).
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

from l3io.wp.backup.cli.parser import build_parser
from l3io.wp.backup.cli.restore_request import render_flags
from l3io.wp.backup.core.restore import MAX_DATABASE_NAME, STAGING_INFIX
from l3io.wp.backup.domain.flag_map import FLAG_MAP, FlagRejection
from l3io.wp.backup.domain.restore import RestoreMode

DOC = Path(__file__).resolve().parents[2] / "docs" / "restore.md"
#: The heading whose section holds the Modes table.
MODES_HEADING = "## Restore Modes"
#: The Run ID characters ``staging_name`` keeps.
RUN_CHARS = 8


def _text() -> str:
    return DOC.read_text(encoding="utf-8")


def _modes_section(text: str) -> list[str]:
    """The lines under :data:`MODES_HEADING`, up to the next heading of its level."""
    lines = text.splitlines()
    assert MODES_HEADING in lines, f"{DOC.name} has no {MODES_HEADING!r} heading"
    start = lines.index(MODES_HEADING) + 1
    end = next(
        (i for i in range(start, len(lines)) if re.match(r"^#{1,2} ", lines[i])),
        len(lines),
    )
    return lines[start:end]


def _mode_rows(text: str) -> dict[str, str]:
    """``{mode: flags}`` from the Modes table's first two columns."""
    rows: dict[str, str] = {}
    for line in _modes_section(text):
        match = re.match(r"^\|\s*`([a-z-]+)`\s*\|\s*`([^`]+)`\s*\|", line)
        if match:
            rows[match.group(1)] = match.group(2)
    return rows


def _selecting_flags() -> dict[str, str]:
    """Each Mode's combination without ``--force``, from ``FLAG_MAP``."""
    expected: dict[str, str] = {}
    for key, entry in FLAG_MAP.items():
        if isinstance(entry, FlagRejection) or dict(key)["force"]:
            continue
        mode, _ = entry
        expected[mode.value] = render_flags(key)
    return expected


def test_the_modes_table_maps_every_mode_to_its_flags() -> None:
    expected = _selecting_flags()
    assert set(expected) == {m.value for m in RestoreMode}
    assert _mode_rows(_text()) == expected


def test_a_wrong_flags_cell_is_caught() -> None:
    planted = (
        f"{MODES_HEADING}\n\n| `database-clean` | `--mode db --cleanfirst` | no |\n"
    )
    assert _mode_rows(planted) != {
        "database-clean": _selecting_flags()["database-clean"]
    }


def test_a_table_outside_the_modes_section_is_not_read() -> None:
    """BL-E007-001: a second table elsewhere cannot add or replace a row."""
    planted = (
        "# Restore\n\n"
        f"{MODES_HEADING}\n\n"
        "| `files` | `--mode files` | yes |\n\n"
        "### A sub-heading keeps the section\n\n"
        "| `all` | `--mode all` | yes |\n\n"
        "## Relocation\n\n"
        "| `files` | `--wrong` | no |\n"
        "| `database` | `--mode db` | no |\n"
    )
    assert _mode_rows(planted) == {"files": "--mode files", "all": "--mode all"}


def _commands(text: str) -> list[list[str]]:
    return [
        shlex.split(line.removeprefix("$ wpbackup2 "))
        for line in text.splitlines()
        if line.startswith("$ wpbackup2 ")
    ]


def test_the_document_shows_commands() -> None:
    """The scope half: the parse check below is not vacuous."""
    commands = _commands(_text())
    assert len(commands) >= 3
    assert any(c[:2] == ["restore", "--whatIf"] for c in commands)


@pytest.mark.parametrize("argv", _commands(_text()), ids=shlex.join)
def test_every_documented_command_parses(argv: list[str]) -> None:
    args = build_parser().parse_args(argv)
    assert args.action == "restore"
    assert args.what_if is ("--whatIf" in argv or "--what-if" in argv)


def test_the_staging_name_is_documented_from_its_rule() -> None:
    live_chars = MAX_DATABASE_NAME - len(STAGING_INFIX) - RUN_CHARS
    text = _text()
    assert (
        f"`<live name, first {live_chars} characters>{STAGING_INFIX}<first "
        f"{RUN_CHARS} characters of the Run ID>`"
    ) in " ".join(text.split())
    assert "the site permanently runs on the staging database's name" in text
