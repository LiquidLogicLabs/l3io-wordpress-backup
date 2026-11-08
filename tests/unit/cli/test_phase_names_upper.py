"""BL-E002-109 (owner decision 2026-11-07) — every logged ``phase`` is upper case.

Restore phases are logged as ``RestorePhase.X.name`` (``LOAD_DUMP``), as a
backup's ``BackupStep`` values are, while the Run Journal keeps its on-disk
``value`` spelling (``load-dump``). The scope is the records real Runs emit,
never a list of call sites: every Restore Mode through the real executor,
real and Dry Run adapter sets, plus a backup and a backup Dry Run. Any record
carrying a ``phase`` key is checked, whatever its event.

The journal half: the file a restore writes still spells phases in lower
case, so an older binary can read it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path

import pytest
from structlog.testing import capture_logs

pytest.importorskip("l3io.wp.database")

from l3io.wp.backup.cli import main
from l3io.wp.backup.cli.adapter_sets import dry_run_adapters
from l3io.wp.backup.domain.restore import RestoreMode, RestorePhase
from tests.restore_harness import Harness, write_v1

UPPER = re.compile(r"^[A-Z][A-Z_]*$")


def _bad(records: Iterable[Mapping[str, object]]) -> list[object]:
    return [
        r["phase"]
        for r in records
        if r.get("phase") is not None and not UPPER.match(str(r["phase"]))
    ]


def _phased(records: Iterable[Mapping[str, object]]) -> list[object]:
    return [r["phase"] for r in records if r.get("phase") is not None]


@pytest.mark.parametrize("dry", [False, True], ids=["real", "dry-run"])
@pytest.mark.parametrize("mode", list(RestoreMode))
def test_a_restore_logs_every_phase_upper_case(
    tmp_path: Path, mode: RestoreMode, dry: bool
) -> None:
    harness = Harness(work=tmp_path, archive_path=write_v1(tmp_path), mode=mode)
    with capture_logs() as logs:
        if dry:
            harness.run(adapters=dry_run_adapters(harness.adapters()))
        else:
            harness.run()
    assert _phased(logs), "the scope is empty: no record carried a phase"
    assert _bad(logs) == []


def test_the_planted_lower_case_spelling_is_caught() -> None:
    planted = [{"event": "phase_completed", "phase": RestorePhase.LOAD_DUMP.value}]
    assert _bad(planted) == ["load-dump"]


@pytest.mark.usefixtures("fake_database")
@pytest.mark.parametrize("what_if", [False, True], ids=["real", "dry-run"])
def test_a_backup_logs_every_phase_upper_case(
    site: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    what_if: bool,
) -> None:
    final = tmp_path / "out.tar.gz"
    argv = ["--log-format", "json", *(["--whatIf"] if what_if else []), "backup"]
    assert main([*argv, "--wp-dir", str(site), "--archive", str(final)]) == 0
    records = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert _phased(records)
    assert _bad(records) == []


def test_the_journal_keeps_its_lower_case_spelling() -> None:
    from l3io.wp.backup.adapters.journal import _entry_dict
    from l3io.wp.backup.domain.journal import JournalEntry

    entry = JournalEntry(
        event="phase_started",
        phase=RestorePhase.LOAD_DUMP,
        at="2026-11-07T00:00:00Z",
        target="x",
        step=None,
    )
    assert _entry_dict(entry)["phase"] == "load-dump"
