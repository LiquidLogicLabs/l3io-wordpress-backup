"""PRD §8 — the structured log schema in ``docs/logging.md`` is what the code emits.

The documented tables (between the ``schema:record`` and ``schema:phase``
markers) are the source of truth, parsed here; the emitted records are those
``configure_logging``'s JSON pipeline writes for a whole ``wpbackup2 backup``
(against a database double). Every record carries the record fields, and every
``phase_completed`` record carries exactly the phase fields beyond them —
except the step-specific fields the phase records itself, which ``phase()``
keeps apart from the fixed set.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from l3io.wp.backup.adapters import phase as phase_module
from l3io.wp.backup.cli import main

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DOC = PROJECT_ROOT / "docs" / "logging.md"
#: Fields ``verify_archive`` adds to its phase record (AD-23), not the schema's.
VERIFY_EXTRAS = frozenset(
    {
        "format_version",
        "integrity",
        "dump_present",
        "passed",
        "authenticated",
        "reason",
        "member",
    }
)


def documented(text: str, name: str) -> set[str]:
    match = re.search(
        rf"<!-- schema:{name} -->(.*?)<!-- /schema:{name} -->", text, re.DOTALL
    )
    assert match is not None, f"docs/logging.md has no schema:{name} table"
    fields = re.findall(r"^\| `([a-z0-9_]+)` \|", match.group(1), re.MULTILINE)
    return set(fields)


def test_the_tables_parse() -> None:
    text = DOC.read_text(encoding="utf-8")
    assert documented(text, "record") == {
        "l3io_run_id", "l3io_dry_run", "timestamp", "level", "logger", "event",
    }  # fmt: skip
    assert len(documented(text, "phase")) >= 4


def test_the_parser_fails_on_a_missing_table() -> None:
    with pytest.raises(AssertionError):
        documented("| `phase` | x |", "phase")


def test_the_documented_phase_fields_are_phases_fixed_set() -> None:
    assert documented(DOC.read_text(encoding="utf-8"), "phase") == set(
        phase_module._FIXED_KEYS  # noqa: SLF001
    )


@pytest.mark.usefixtures("fake_database")
def test_every_emitted_record_matches_the_schema(
    site: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = DOC.read_text(encoding="utf-8")
    record, phase = documented(text, "record"), documented(text, "phase")
    final = tmp_path / "out.tar.gz"
    argv = ["--log-format", "json", "backup", "--wp-dir", str(site)]
    assert main([*argv, "--archive", str(final)]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    phases = [line for line in lines if line["event"] == "phase_completed"]
    assert len(phases) == 5
    for line in lines:
        assert record <= line.keys(), line
    for line in phases:
        # Only VERIFY records step-specific fields; every other phase is exact.
        extras = VERIFY_EXTRAS if line["phase"] == "VERIFY" else frozenset()
        assert set(line) - record - extras == phase, line
    (verify,) = [p for p in phases if p["phase"] == "VERIFY"]
    assert set(verify) - record - phase <= VERIFY_EXTRAS


def test_a_run_binds_only_prefixed_keys() -> None:
    """BL-E006-019 (owner decision 2026-11-07): a host's own keys survive a Run.

    The scope is whatever the Run binds, read back from structlog's context:
    every key it adds is ``l3io_``-prefixed, and a host's ``run_id`` and
    ``dry_run`` are the host's values throughout and after the call.
    """
    import structlog

    from l3io.wp.backup.cli.context import open_run

    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(run_id="host-run", dry_run="host")
    try:
        before = structlog.contextvars.get_contextvars()
        with open_run(operation="verify") as ctx:
            during = structlog.contextvars.get_contextvars()
        after = structlog.contextvars.get_contextvars()
    finally:
        structlog.contextvars.clear_contextvars()
    added = set(during) - set(before)
    assert added == {"l3io_run_id", "l3io_dry_run"}
    assert all(key.startswith("l3io_") for key in added)
    assert during["l3io_run_id"] == ctx.run_id
    assert during["run_id"] == "host-run"
    assert during["dry_run"] == "host"
    assert after == before
