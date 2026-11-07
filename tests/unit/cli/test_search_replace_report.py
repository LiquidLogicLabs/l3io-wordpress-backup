"""E007-S01-003 (FR-12) — the restore report states the content URL rewrite.

The AC: "the rewrite is reported: replacements made, tables touched", and the
pairs applied. The text line is ``content URLs: <n> replacements in <k>
tables (<old> → <new>)``; the JSON report carries the same fields. A Dry Run's
report has no counts (``None``), never a guessed number.
"""

from __future__ import annotations

from l3io.wp.backup.cli.restore import search_replace_fields, search_replace_line
from l3io.wp.backup.domain.search_replace import (
    UNRESOLVED_URL,
    Replacement,
    SearchReplaceReport,
)

OLD, NEW = "http://old.example.com", "https://new.example.org"
DONE = SearchReplaceReport(
    pairs=(Replacement(OLD, NEW),),
    replacements=7,
    tables=("wp_options", "wp_posts"),
    remaining=0,
    merged=True,
)
PLANNED = SearchReplaceReport(
    pairs=(Replacement(UNRESOLVED_URL, NEW),),
    replacements=None,
    tables=None,
    remaining=None,
    merged=False,
)


def test_the_line_names_count_tables_and_pairs() -> None:
    assert search_replace_line(DONE) == (
        f"content URLs: 7 replacements in 2 tables ({OLD} → {NEW})"
    )


def test_a_dry_run_line_names_the_planned_pairs_and_no_count() -> None:
    line = search_replace_line(PLANNED)
    assert f"{UNRESOLVED_URL} → {NEW}" in line
    assert "counts determined after LOAD_DUMP" in line
    assert "None" not in line


def test_the_json_fields_carry_tables_and_pairs() -> None:
    assert search_replace_fields(DONE) == {
        "replacements": 7,
        "tables": ["wp_options", "wp_posts"],
        "pairs": [{"old": OLD, "new": NEW}],
        "remaining": 0,
        "merged": True,
    }
    planned = search_replace_fields(PLANNED)
    assert planned["replacements"] is None
    assert planned["tables"] is None
