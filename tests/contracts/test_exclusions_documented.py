"""FR-5 — ``docs/exclusions.md``'s default-set table is ``DEFAULT_EXCLUSIONS``.

The table between the generated-content markers is parsed and compared with
the constant in order, so the documentation cannot drift from the code.

**Not covered:** the "why" column and the prose around the table.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from l3io.wp.backup.core.exclusions import DEFAULT_EXCLUSIONS

DOC = Path(__file__).resolve().parents[2] / "docs" / "exclusions.md"
BEGIN = re.compile(r"^<!-- BEGIN GENERATED: default-exclusions\b.*-->$", re.MULTILINE)
END = "<!-- END GENERATED: default-exclusions -->"
ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|", re.MULTILINE)


def documented_patterns(text: str) -> tuple[str, ...]:
    """The first-column patterns of the generated table, in order."""
    begin = BEGIN.search(text)
    if begin is None or text.count(END) != 1 or len(BEGIN.findall(text)) != 1:
        msg = "exactly one default-exclusions generated block is required"
        raise AssertionError(msg)
    block = text[begin.end() : text.index(END)]
    return tuple(ROW.findall(block))


def test_the_documented_table_is_the_constant_in_order() -> None:
    assert documented_patterns(DOC.read_text(encoding="utf-8")) == DEFAULT_EXCLUSIONS


@pytest.mark.parametrize(
    "mutate",
    [
        lambda rows: rows[:-1],
        lambda rows: [rows[1], rows[0], *rows[2:]],
        lambda rows: [*rows, "| `wp-content/uploads/` | planted |"],
    ],
    ids=["dropped", "reordered", "added"],
)
def test_the_guard_fires_on_a_drifted_table(mutate: object) -> None:
    text = DOC.read_text(encoding="utf-8")
    begin = BEGIN.search(text)
    assert begin is not None
    block = text[begin.end() : text.index(END)]
    rows = [line for line in block.splitlines() if ROW.match(line)]
    drifted = mutate(rows)  # type: ignore[operator]
    planted = text.replace(block, "\n" + "\n".join(drifted) + "\n")
    assert documented_patterns(planted) != DEFAULT_EXCLUSIONS


def test_a_row_outside_the_markers_is_not_counted() -> None:
    """Scope: only the generated block, so a stray table cannot satisfy the guard."""
    text = DOC.read_text(encoding="utf-8")
    begin = BEGIN.search(text)
    assert begin is not None
    block = text[begin.end() : text.index(END)]
    moved = text.replace(block, "\n") + block
    assert ROW.findall(moved) != []
    assert documented_patterns(moved) == ()
