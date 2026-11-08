"""Phases join by identity, not by their logged name (BL-E002-109 regression).

Backup's ``BackupStep.VERIFY`` and restore's ``RestorePhase.VERIFY`` are both
logged ``VERIFY``. A step that joins "the open VERIFY" must join only the
phase it belongs to: a backup verification running inside a restore's
``VERIFY`` (a Safety Snapshot's, say) opens its own phase, nested under the
restore's, and never annotates the restore's record.
"""

from __future__ import annotations

from typing import Any

import pytest
from structlog.testing import capture_logs

from l3io.wp.backup.adapters.phase import (
    PhaseKey,
    join_open_phase,
    join_phase,
    phase,
    phase_name,
)
from l3io.wp.backup.core.backup import BackupStep
from l3io.wp.backup.domain.restore import RestorePhase


def _completed(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in logs if e["event"] == "phase_completed"]


# Every name the two enums share: each pair is two distinct phases.
SHARED = sorted(
    {s.value for s in BackupStep} & {p.name for p in RestorePhase},
)


def test_the_enums_still_share_a_name() -> None:
    # Without a shared name this file would test nothing.
    assert "VERIFY" in SHARED


@pytest.mark.parametrize("name", SHARED)
@pytest.mark.parametrize("outer_is_restore", [True, False], ids=["restore", "backup"])
def test_a_same_named_phase_of_the_other_kind_is_not_joined(
    name: str, *, outer_is_restore: bool
) -> None:
    restore_key: PhaseKey = RestorePhase[name]
    backup_key: PhaseKey = BackupStep(name)
    outer, inner = (
        (restore_key, backup_key) if outer_is_restore else (backup_key, restore_key)
    )
    with capture_logs() as logs, phase(outer) as outer_record:
        outer_record.fields["sentinel_outer_7c1e"] = True
        with join_phase(inner) as inner_record:
            assert inner_record is not outer_record
            inner_record.fields["sentinel_inner_9b2d"] = True
    records = _completed(logs)
    # Two records, both logged under the shared name, each with only its own field.
    assert [r["phase"] for r in records] == [name, name]
    inner_log, outer_log = records
    assert "sentinel_inner_9b2d" in inner_log
    assert "sentinel_outer_7c1e" not in inner_log
    assert "sentinel_outer_7c1e" in outer_log
    assert "sentinel_inner_9b2d" not in outer_log


@pytest.mark.parametrize("key", [RestorePhase.VERIFY, BackupStep.VERIFY, "JOURNAL"])
def test_the_same_phase_is_joined(key: PhaseKey) -> None:
    with capture_logs() as logs, phase(key) as outer, join_phase(key) as joined:
        assert joined is outer
    assert len(_completed(logs)) == 1


def test_a_plain_name_does_not_join_an_enum_phase_of_that_name() -> None:
    with (
        capture_logs() as logs,
        phase(BackupStep.VERIFY) as outer,
        join_phase(BackupStep.VERIFY.value) as inner,
    ):
        assert inner is not outer
    assert len(_completed(logs)) == 2


def test_join_open_phase_joins_the_innermost_whatever_its_key() -> None:
    with (
        capture_logs() as logs,
        phase(RestorePhase.CLEAN) as outer,
        join_open_phase(BackupStep.DUMP) as joined,
    ):
        assert joined is outer
    assert [r["phase"] for r in _completed(logs)] == ["CLEAN"]


def test_logged_names_keep_decision_11_spelling() -> None:
    assert phase_name(RestorePhase.LOAD_DUMP) == "LOAD_DUMP"
    assert phase_name(BackupStep.WRITE_ARCHIVE) == "WRITE_ARCHIVE"
    assert phase_name("JOURNAL") == "JOURNAL"
