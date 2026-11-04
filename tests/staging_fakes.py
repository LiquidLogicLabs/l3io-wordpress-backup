"""Fakes for ``adapters/staging.py``'s slots (E003-S02-005). Not a test module.

:class:`FakeDatabase` stands in for both restore database halves and records
every call, query or command, in one ordered list. :class:`StagedHarness` is
``tests/restore_harness.py``'s :class:`~tests.restore_harness.Harness` with
the real ``load_staged``/``check_staged`` bound over that fake, and a journal
header built the way the restore root builds it (from ``core/restore.py``'s
``load_names``/``planned_cutover_*``). :class:`EntryJournal` keeps the
``JournalEntry`` values a Run wrote.
"""

from __future__ import annotations

import functools
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import IO

from l3io.wp.backup.adapters.restore_runner import execute_restore
from l3io.wp.backup.adapters.staging import check_staged, load_staged
from l3io.wp.backup.core import restore as core_restore
from l3io.wp.backup.core.restore import (
    STEP_CHECK_FAILED,
    STEP_CONFIG,
    STEP_CONFIG_STARTED,
    STEP_CUTOVER,
    STEP_CUTOVER_READBACK_FAILED,
    STEP_CUTOVER_STARTED,
    STEP_LOSER_DROP_STARTED,
    STEP_LOSER_DROPPED,
    STEP_STAGING_DROPPED,
    ExecutionReport,
    RestoreRelocation,
    load_names,
    planned_cutover_keys,
    planned_cutover_targets,
)
from l3io.wp.backup.domain.config import ConfigWriteResult
from l3io.wp.backup.domain.database import (
    DatabaseLocation,
    DatabaseTargetState,
    DumpResult,
    EnsureOutcome,
    LoadResult,
    Readiness,
)
from l3io.wp.backup.domain.errors import ConfigWriteFailedError
from l3io.wp.backup.domain.journal import (
    JOURNALED_CONSTANTS,
    JournalEntry,
    JournalEvent,
    JournalHeader,
)
from l3io.wp.backup.domain.restore import RestoreMode, RestorePhase
from l3io.wp.backup.ports.adapter_set import AdapterSet
from l3io.wp.backup.ports.journal import RunJournalCommand
from l3io.wp.backup.ports.restore import RestorePorts
from tests.restore_harness import TARGET_DB, CommandLog, FakeConfig, Harness

READY = Readiness(ready=True)
WORDPRESS = DatabaseTargetState(True, 12, True, "57155", True)
NOT_WORDPRESS = DatabaseTargetState(True, 3, False, None, True)
RUN_ID = "run-1"


@dataclass
class FakeDatabase:
    """``DatabaseQuery`` and ``DatabaseCommand``; every call recorded in order."""

    calls: list[tuple[str, str, str]] = field(default_factory=list)
    readiness: dict[str, Readiness] = field(default_factory=dict)
    survey: DatabaseTargetState = WORDPRESS
    created_database: bool = True
    performed: bool = True
    failures: dict[str, BaseException] = field(default_factory=dict)
    log: CommandLog | None = None
    administrator_bound: bool = True

    def _call(self, kind: str, method: str, location: DatabaseLocation) -> None:
        self.calls.append((kind, method, location.name))
        if self.log is not None:
            self.log.add("database", method, location.name)
        failure = self.failures.get(method)
        if failure is not None:
            raise failure

    def methods(self, kind: str | None = None) -> list[str]:
        return [m for k, m, _ in self.calls if kind is None or k == kind]

    # DatabaseQuery
    def check_dump(self, location: DatabaseLocation) -> Readiness:
        self._call("query", "check_dump", location)
        return READY

    def survey_target(
        self,
        location: DatabaseLocation,
        *,
        table_prefix: str,
        wordpress_user: str | None,
    ) -> DatabaseTargetState:
        self._call("query", "survey_target", location)
        return self.survey

    def _ready(self, method: str, location: DatabaseLocation) -> Readiness:
        self._call("query", method, location)
        return self.readiness.get(method, READY)

    def check_ensure(self, location: DatabaseLocation) -> Readiness:
        return self._ready("check_ensure", location)

    def check_load(
        self, location: DatabaseLocation, *, source: Path | None
    ) -> Readiness:
        return self._ready("check_load", location)

    def check_revoke(
        self, location: DatabaseLocation, *, user: str, confirm: str
    ) -> Readiness:
        return self._ready("check_revoke", location)

    def check_drop_database(
        self, location: DatabaseLocation, *, confirm: str
    ) -> Readiness:
        assert confirm == location.name
        return self._ready("check_drop_database", location)

    # DatabaseCommand
    def dump_to(self, location: DatabaseLocation, destination: Path) -> DumpResult:
        self._call("command", "dump_to", location)
        return DumpResult(bytes=0, duration_ms=0.0)

    def ensure_database(self, location: DatabaseLocation) -> EnsureOutcome:
        self._call("command", "ensure_database", location)
        return EnsureOutcome(
            self.created_database, False, False, True, (), performed=self.performed
        )

    def load_dump(
        self, location: DatabaseLocation, source: IO[bytes] | Path
    ) -> LoadResult:
        self._call("command", "load_dump", location)
        return LoadResult(bytes=10, duration_ms=1.0, performed=self.performed)

    def revoke_grants(
        self, location: DatabaseLocation, *, user: str, confirm: str
    ) -> bool:
        self._call("command", "revoke_grants", location)
        return True

    def drop_database(self, location: DatabaseLocation, *, confirm: str) -> bool:
        assert confirm == location.name
        self._call("command", "drop_database", location)
        return True


class FailingConfig(FakeConfig):
    """``FakeConfig`` whose ``set_constants`` fails for ``failing`` keys."""

    def __init__(
        self, log: CommandLog, values: Mapping[str, str], failing: frozenset[str]
    ) -> None:
        super().__init__(log, values)
        self.failing = failing

    def set_constants(
        self, config_file: Path, values: Mapping[str, str | int | bool]
    ) -> ConfigWriteResult:
        for key in values:
            if key in self.failing:
                raise ConfigWriteFailedError(config_file, key, "read-back mismatch")
        return super().set_constants(config_file, values)


class EntryJournal:
    """``RunJournalCommand`` keeping the ``JournalEntry`` values written."""

    def __init__(self, log: CommandLog | None = None) -> None:
        self.log = log
        self.header: JournalHeader | None = None
        self.entries: list[JournalEntry] = []
        self.finished = False

    def _add(
        self,
        event: JournalEvent,
        phase: RestorePhase,
        target: str | None = None,
        step: str | None = None,
    ) -> None:
        at = datetime.now(UTC).isoformat()
        self.entries.append(JournalEntry(event, phase, at, target, step))
        if self.log is not None:
            self.log.add("journal", event, step or phase.value)

    def begin_run(self, header: JournalHeader) -> None:
        self.header = header

    def phase_started(self, phase: RestorePhase, target: str) -> None:
        self._add("phase_started", phase, target=target)

    def step_completed(self, phase: RestorePhase, step: str) -> None:
        self._add("step_completed", phase, step=step)

    def phase_completed(self, phase: RestorePhase) -> None:
        self._add("phase_completed", phase)

    def finish_run(self) -> None:
        self.finished = True

    def resolve(self, run_id: str, resolved_by: str) -> None:
        msg = "not under test"
        raise AssertionError(msg)

    def steps(self) -> list[str]:
        return [e.step for e in self.entries if e.step is not None]


@dataclass
class StagedHarness(Harness):
    """``Harness`` with the real staging slots over :class:`FakeDatabase`."""

    database: FakeDatabase = field(default_factory=FakeDatabase)
    failing_keys: frozenset[str] = frozenset()
    password: str | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        self.database.log = self.log
        if self.failing_keys:
            self.config = FailingConfig(self.log, TARGET_DB, self.failing_keys)

    def adapters(self) -> AdapterSet:
        import dataclasses

        base = super().adapters()
        return dataclasses.replace(
            base,
            restore_database_query=self.database,
            restore_database_command=self.database,
        )

    def ports(self, adapters: AdapterSet | None = None) -> RestorePorts:
        import dataclasses

        ports = super().ports(adapters)
        if self.password is None:
            return ports
        password = self.password
        return dataclasses.replace(ports, cutover_credentials=lambda: password)

    def header(self) -> JournalHeader:
        return staged_header(
            run_id=RUN_ID,
            site_root=PurePosixPath(self.site_root),
            live=self.site.db_name,
            relocation=self.relocation,
            mode=self.mode,
            archive=str(self.archive_path),
            config_path=str(self.relocation.config_file or self.config_path),
            snapshot_path=str(self.snapshot_file),
        )

    def run_staged(
        self,
        journal: RunJournalCommand | None = None,
        adapters: AdapterSet | None = None,
    ) -> ExecutionReport:
        ports = self.ports(adapters)
        return execute_restore(
            self.plan(),
            self.inputs(),
            ports,
            self.take_snapshot,
            functools.partial(check_staged, ports=ports),
            functools.partial(load_staged, ports=ports, workspace=self.workspace),
            journal or self.journal,
            self.cipher,
            survey_target=self.target,
            opted_out_by=self.opted_out_by,
        )


def staged_header(
    *,
    run_id: str,
    site_root: PurePosixPath,
    live: str,
    relocation: RestoreRelocation,
    mode: RestoreMode,
    archive: str,
    config_path: str,
    snapshot_path: str | None = None,
    prior: Mapping[str, str | None] | None = None,
) -> JournalHeader:
    """The header the restore root builds before ``SNAPSHOT`` (ADR-0021)."""
    load, staging = load_names(live, run_id, relocation)
    prior_values = dict(prior) if prior is not None else dict(TARGET_DB)
    return JournalHeader(
        run_id=run_id,
        started_at="2026-11-03T00:00:00+00:00",
        site_root=site_root,
        restore_mode=mode,
        archive=archive,
        snapshot_path=snapshot_path,
        live_database_before=live,
        staging_database=staging,
        wp_config_path=config_path,
        load_database=load,
        cutover_keys=planned_cutover_keys(relocation),
        cutover_targets=planned_cutover_targets(load, relocation),
        prior_constants={
            k: v for k, v in prior_values.items() if k in JOURNALED_CONSTANTS
        },
        phase_undo={},
        saved_config=None,
    )


# --------------------------------------------------------------------------
# The undo table's rows, as an oracle independent of ``core/restore.py``
# --------------------------------------------------------------------------

#: ADR-0021's file phases, stated independently of ``core/restore.py``: once
#: one started, files under the Site Root may have been replaced.
FILE_PHASES = frozenset(
    {RestorePhase.CLEAN, RestorePhase.EXTRACT, RestorePhase.REWRITE_CONFIG}
)
_PRE_SITE = {None, RestorePhase.PREFLIGHT, RestorePhase.VERIFY, RestorePhase.SNAPSHOT}


def _snapshot(header: JournalHeader) -> str:
    if header.snapshot_path is None:
        return "no Safety Snapshot: opted out"
    return f"restore files from Safety Snapshot {header.snapshot_path}"


def _files(header: JournalHeader) -> str:
    return (
        "the live database is untouched, but files were replaced by this Run: "
        f"{_snapshot(header)}"
    )


def _load_row(step: str | None, header: JournalHeader, *, files_changed: bool) -> str:
    load, before = header.load_database, header.live_database_before
    keys = header.cutover_keys
    untouched = _files(header) if files_changed else "live site untouched"
    if step in {None, STEP_CHECK_FAILED, STEP_CUTOVER_READBACK_FAILED}:
        if header.staging_database is None:
            named = f"{load} existed and was empty before this Run; empty or drop it"
            return f"{untouched}; {named}" if files_changed else named
        return f"{untouched}; drop staging database {load}"
    assert step is not None
    if step == STEP_STAGING_DROPPED:
        return f"{untouched}; the staging database was already dropped"
    if step.startswith(STEP_CUTOVER_STARTED):
        key = step.removeprefix(STEP_CUTOVER_STARTED)
        return (
            f"the cutover was writing {key} and it may have landed; do not drop {load}"
        )
    if step.startswith(STEP_CUTOVER):
        key = step.removeprefix(STEP_CUTOVER)
        if key in keys and key != keys[-1]:
            return f"set {', '.join(keys[: keys.index(key) + 1])} back to"
        return (
            f"the site now runs on {load}; the previous database {before} is retained"
        )
    if step == STEP_LOSER_DROP_STARTED:
        return (
            f"the site runs on {load}; removal of the previous database {before} "
            "was in progress"
        )
    if step == STEP_LOSER_DROPPED:
        return f"the site runs on {load}; {before} was dropped, as this Mode names"
    return "unrecognised step"


def undo_row(
    phase: RestorePhase | None,
    step: str | None,
    header: JournalHeader,
    *,
    files_changed: bool,
) -> str:
    """The undo table's row for ``(phase, step)``: the text the undo starts with."""
    config = header.wp_config_path
    if phase in _PRE_SITE:
        return "nothing on the Site was changed"
    if phase is RestorePhase.EXTRACT and step == STEP_CONFIG_STARTED:
        return f"remove {config} if present (this Run was laying it down"
    if phase is RestorePhase.EXTRACT and step == STEP_CONFIG:
        return f"remove {config} (laid down by this Run; none existed)"
    if phase in {RestorePhase.CLEAN, RestorePhase.EXTRACT}:
        return _snapshot(header)
    if phase is RestorePhase.REWRITE_CONFIG and step and step.startswith("config_"):
        return f"copy {header.saved_config} back to {config}"
    if phase in {RestorePhase.REWRITE_CONFIG, RestorePhase.SEARCH_REPLACE}:
        assert phase is not None
        return header.phase_undo.get(phase) or f"{config} unchanged"
    assert phase is RestorePhase.LOAD_DUMP
    return _load_row(step, header, files_changed=files_changed)


def undo_rows(header: JournalHeader) -> frozenset[str]:
    """Every row :func:`undo_row` can give for ``header``.

    Over every ``STEP_*`` constant ``core/restore.py`` declares (the source
    of truth), each cutover step of ``header.cutover_keys``, no step and an
    unknown one.
    """
    declared = [v for n, v in vars(core_restore).items() if n.startswith("STEP_")]
    steps: list[str | None] = [
        None,
        "unknown",
        *declared,
        *(STEP_CUTOVER_STARTED + key for key in header.cutover_keys),
        *(STEP_CUTOVER + key for key in header.cutover_keys),
    ]
    return frozenset(
        undo_row(phase, step, header, files_changed=files)
        for phase in [None, *RestorePhase]
        for step in steps
        for files in (False, True)
    )


def assert_one_row(
    undo: str,
    phase: RestorePhase | None,
    step: str | None,
    header: JournalHeader,
    *,
    files_changed: bool,
) -> None:
    """``undo`` starts with its own row and with no other row of the table."""
    expected = undo_row(phase, step, header, files_changed=files_changed)
    matching = sorted(row for row in undo_rows(header) if undo.startswith(row))
    assert matching == [expected], (phase, step, files_changed, undo, matching)


__all__ = [
    "FILE_PHASES",
    "NOT_WORDPRESS",
    "READY",
    "RUN_ID",
    "WORDPRESS",
    "EntryJournal",
    "FailingConfig",
    "FakeDatabase",
    "StagedHarness",
    "assert_one_row",
    "staged_header",
    "undo_row",
    "undo_rows",
]
