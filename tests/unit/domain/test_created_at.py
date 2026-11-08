"""BL-E002-005 (owner decision 28) — one ``created_at`` spelling written, two read.

New Manifests write ISO-8601 UTC with whole seconds and a ``Z`` suffix
(``domain/archive_format.created_at_text``, the one serializer). Readers accept
that form and the earlier ``+00:00`` form with or without a fraction, which
existing v1 Archives and the committed fixtures carry.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from l3io.wp.backup.domain.archive_format import created_at_text, parse_created_at

PACKAGE = Path(__file__).resolve().parents[3] / "src" / "l3io" / "wp" / "backup"
OWNER = PACKAGE / "domain" / "archive_format.py"

INSTANT = datetime(2026, 11, 7, 21, 4, 5, tzinfo=UTC)


@pytest.mark.parametrize(
    "when",
    [
        INSTANT,
        INSTANT.replace(microsecond=987_654),
        INSTANT.astimezone(timezone(timedelta(hours=-5))),
        INSTANT.replace(microsecond=1).astimezone(timezone(timedelta(hours=9))),
    ],
    ids=["utc", "fraction", "negative-offset", "positive-offset-fraction"],
)
def test_the_write_form_is_utc_whole_seconds_with_z(when: datetime) -> None:
    assert created_at_text(when) == "2026-11-07T21:04:05Z"


@pytest.mark.parametrize(
    "text",
    [
        "2026-11-07T21:04:05Z",
        "2026-11-07T21:04:05+00:00",
        "2026-11-07T21:04:05.000000+00:00",
        "2026-11-07T16:04:05-05:00",
    ],
)
def test_both_read_forms_are_accepted(text: str) -> None:
    assert parse_created_at(text) == INSTANT
    assert parse_created_at(text).tzinfo is UTC


def test_a_fraction_survives_reading() -> None:
    parsed = parse_created_at("2026-11-07T21:04:05.250000+00:00")
    assert parsed == INSTANT.replace(microsecond=250_000)


@pytest.mark.parametrize("text", ["2026-11-07T21:04:05", "yesterday", ""])
def test_a_value_without_an_offset_or_not_iso_is_refused(text: str) -> None:
    with pytest.raises(ValueError, match=r"created_at|Invalid isoformat"):
        parse_created_at(text)


def test_the_written_form_reads_back() -> None:
    assert parse_created_at(created_at_text(INSTANT)) == INSTANT


def test_nothing_outside_the_owner_formats_created_at_itself() -> None:
    """One serializer: no ``created_at=`` keyword is built by ``.isoformat()``."""
    hits: list[str] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        if path == OWNER:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.keyword) or node.arg != "created_at":
                continue
            for inner in ast.walk(node.value):
                if isinstance(inner, ast.Attribute) and inner.attr in {
                    "isoformat",
                    "strftime",
                }:
                    hits.append(f"{path.relative_to(PACKAGE)}:{node.value.lineno}")
    assert hits == []
