"""AD-18.1 — the Run Journal's values: no secret can be journaled.

``JournalHeader`` refuses any key outside ``JOURNALED_CONSTANTS`` in
``cutover_targets`` and ``prior_constants`` — ``DB_PASSWORD`` first among
them — at construction, and never shows a refused value. The values
round-trip through ``dataclasses.asdict``; ``restore_mode`` and ``phase`` are
typed ``RestoreMode``/``RestorePhase`` (mypy holds the members-only half).
"""

from __future__ import annotations

import copy
import dataclasses
import pickle
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Any

import pytest

from l3io.wp.backup.domain.journal import (
    JOURNALED_CONSTANTS,
    JournalEntry,
    JournalHeader,
    JournalRecord,
)
from l3io.wp.backup.domain.restore import RestoreMode, RestorePhase


def _header(**overrides: object) -> JournalHeader:
    fields: dict[str, object] = {
        "run_id": "r-1",
        "started_at": "2026-11-03T00:00:00Z",
        "site_root": PurePosixPath("/srv/www"),
        "restore_mode": RestoreMode.ALL_CLEAN,
        "archive": "/backups/site.tar.gz",
        "snapshot_path": None,
        "live_database_before": "wp",
        "staging_database": "wp_stage",
        "wp_config_path": "/srv/www/wp-config.php",
        "load_database": "wp_stage",
        "cutover_keys": ("DB_NAME",),
        "cutover_targets": {"DB_NAME": "wp_stage"},
        "prior_constants": {"DB_NAME": "wp", "DB_HOST": None},
        "phase_undo": {RestorePhase.SEARCH_REPLACE: "wp search-replace new old"},
        "saved_config": None,
    }
    fields.update(overrides)
    return JournalHeader(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["cutover_targets", "prior_constants"])
@pytest.mark.parametrize("key", ["DB_PASSWORD", "AUTH_KEY", "db_name"])
def test_a_key_outside_the_journaled_constants_is_refused(field: str, key: str) -> None:
    secret = "never-shown-7f3a"
    with pytest.raises(ValueError, match=field) as caught:
        _header(**{field: {"DB_NAME": "wp", key: secret}})
    assert key in str(caught.value)
    assert secret not in str(caught.value)


def test_every_journaled_constant_is_accepted() -> None:
    every = dict.fromkeys(sorted(JOURNALED_CONSTANTS), "x")
    header = _header(cutover_targets=every, prior_constants=every)
    assert set(header.cutover_targets) == JOURNALED_CONSTANTS
    assert "DB_PASSWORD" not in JOURNALED_CONSTANTS


def test_the_header_copies_its_mappings() -> None:
    targets = {"DB_NAME": "wp_stage"}
    header = _header(cutover_targets=targets)
    targets["DB_PASSWORD"] = "late"
    assert dict(header.cutover_targets) == {"DB_NAME": "wp_stage"}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m.__setitem__("DB_PASSWORD", "s"),
        lambda m: m.update(DB_PASSWORD="s"),
        lambda m: m.setdefault("DB_PASSWORD", "s"),
        lambda m: m.pop("DB_NAME"),
        lambda m: m.clear(),
    ],
    ids=["setitem", "update", "setdefault", "pop", "clear"],
)
@pytest.mark.parametrize("field", ["cutover_targets", "prior_constants"])
def test_no_key_can_be_added_after_construction(
    field: str, mutate: Callable[[Any], object]
) -> None:
    header = _header()
    with pytest.raises(TypeError, match="read-only"):
        mutate(getattr(header, field))
    assert "DB_PASSWORD" not in getattr(header, field)


def test_a_header_is_hashable_and_copies() -> None:
    header = _header()
    assert hash(header) == hash(_header())
    assert copy.deepcopy(header) == header
    assert pickle.loads(pickle.dumps(header)) == header  # noqa: S301 - own value


def test_the_values_round_trip_through_asdict() -> None:
    header = _header()
    entry = JournalEntry(
        event="step_completed",
        phase=RestorePhase.LOAD_DUMP,
        at="2026-11-03T00:00:01Z",
        target="wp_stage",
        step="load",
    )
    record = JournalRecord(header=header, entries=(entry,))
    assert JournalHeader(**dataclasses.asdict(header)) == header
    assert JournalEntry(**dataclasses.asdict(entry)) == entry
    raw = dataclasses.asdict(record)
    rebuilt = JournalRecord(
        header=JournalHeader(**raw["header"]),
        entries=tuple(JournalEntry(**e) for e in raw["entries"]),
    )
    assert rebuilt == record
    assert isinstance(rebuilt.header.restore_mode, RestoreMode)
    assert isinstance(rebuilt.entries[0].phase, RestorePhase)


def test_an_entry_stores_no_undo() -> None:
    assert {f.name for f in dataclasses.fields(JournalEntry)} == {
        "event",
        "phase",
        "at",
        "target",
        "step",
    }
