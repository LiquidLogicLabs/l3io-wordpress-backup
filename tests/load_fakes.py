"""Fakes for ``adapters/database_load.py``'s slots. Not a test module.

:class:`FakeDatabase` stands in for both restore database halves and records
every call, query or command, in one ordered list. :class:`LoadHarness` is
``tests/restore_harness.py``'s :class:`~tests.restore_harness.Harness` with
the real ``load_live``/``check_live`` bound over that fake, and a journal
header built the way the restore root builds it (from ``core/restore.py``'s
``load_database_name``/``planned_cutover_*``). :class:`EntryJournal` keeps the
``JournalEntry`` values a Run wrote. A rollback reads the Safety Snapshot the
harness's ``take_snapshot`` wrote (a real Archive holding a Dump).
"""

from __future__ import annotations

import functools
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import IO

from l3io.wp.backup.adapters.database import ADMIN_FLAGS, ADMIN_SOURCE
from l3io.wp.backup.adapters.database_load import (
    check_live,
    load_live,
    roll_back_relocation,
)
from l3io.wp.backup.adapters.restore_runner import execute_restore
from l3io.wp.backup.adapters.site_files import LocalSiteFiles
from l3io.wp.backup.core import restore_journal as core_restore
from l3io.wp.backup.core.restore import (
    ExecutionReport,
    RestoreInputs,
    RestoreRelocation,
    RollbackKind,
    SnapshotResult,
    database_rollback,
    load_database_name,
    planned_cutover_keys,
    planned_cutover_targets,
)
from l3io.wp.backup.core.restore_journal import (
    STEP_CHECK_FAILED,
    STEP_CONFIG,
    STEP_CONFIG_STARTED,
    STEP_CUTOVER,
    STEP_CUTOVER_READBACK_FAILED,
    STEP_CUTOVER_STARTED,
    STEP_DATABASE_CREATED,
    STEP_LOAD_STARTED,
    STEP_LOAD_TARGET_DROPPED,
    STEP_LOADED,
    STEP_LOSER_DROP_STARTED,
    STEP_LOSER_DROPPED,
    STEP_ROLLBACK_FAILED,
    STEP_ROLLBACK_STARTED,
    STEP_ROLLED_BACK,
    STEP_STAGING_DROPPED,
)
from l3io.wp.backup.domain.archive_format import (
    ArchiveView,
    ExtractResult,
    Integrity,
    MemberRole,
)
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.config import ConfigWriteResult
from l3io.wp.backup.domain.database import (
    DatabaseLocation,
    DatabaseTargetState,
    DumpResult,
    EnsureOutcome,
    LoadResult,
    Readiness,
)
from l3io.wp.backup.domain.errors import (
    AdminCredentialsRequiredError,
    ConfigWriteFailedError,
)
from l3io.wp.backup.domain.journal import (
    JOURNALED_CONSTANTS,
    JournalEntry,
    JournalEvent,
    JournalHeader,
)
from l3io.wp.backup.domain.restore import RestoreMode, RestorePhase
from l3io.wp.backup.domain.storage_target import StorageTarget
from l3io.wp.backup.ports.adapter_set import AdapterSet
from l3io.wp.backup.ports.clock import Clock
from l3io.wp.backup.ports.journal import RunJournalCommand
from l3io.wp.backup.ports.restore import RestorePorts
from l3io.wp.backup.ports.site_files import SiteFilesCommand
from tests.restore_harness import TARGET_DB, CommandLog, FakeConfig, Harness, write_v1

READY = Readiness(ready=True)
WORDPRESS = DatabaseTargetState(
    True, 12, True, "57155", True, 0, 0, prefixed_tables=("wp_options", "wp_users")
)
NOT_WORDPRESS = DatabaseTargetState(True, 3, False, None, True, 0, 0, ())
#: What ``check_load`` answers for an account an administrator must provision.
NEEDS_ADMIN = Readiness(ready=False, blockers=("database_absent",))
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
    #: Each ``load_dump``'s source, ``replace`` and ``prune_prefix``, in order.
    loads: list[tuple[Path, bool, str | None]] = field(default_factory=list)
    #: The tables ``load_dump`` reports it dropped first.
    dropped: tuple[str, ...] = ("wp_options", "wp_users", "wp_extra")
    #: ``load_dump`` fails only on these calls (1-based); every call when empty.
    fail_loads: frozenset[int] = frozenset()

    def _call(self, kind: str, method: str, location: DatabaseLocation) -> None:
        self.calls.append((kind, method, location.name))
        if self.log is not None:
            self.log.add("database", method, location.name)
        failure = self.failures.get(method)
        if failure is not None and method != "load_dump":
            raise failure
        if failure is not None and (
            not self.fail_loads or len(self.loads) in self.fail_loads
        ):
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
        if not self.administrator_bound:
            self._call("query", "check_ensure", location)
            raise AdminCredentialsRequiredError(ADMIN_SOURCE, flags=ADMIN_FLAGS)
        return self._ready("check_ensure", location)

    def check_load(
        self,
        location: DatabaseLocation,
        *,
        source: Path | None,
        replace: bool = False,
        prune_prefix: str | None = None,
    ) -> Readiness:
        assert replace, "a restore always loads over the live database"
        assert prune_prefix == "wp_", prune_prefix
        return self._ready("check_load", location)

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
        self,
        location: DatabaseLocation,
        source: IO[bytes] | Path,
        *,
        replace: bool = False,
        prune_prefix: str | None = None,
    ) -> LoadResult:
        assert isinstance(source, Path)
        self.loads.append((source, replace, prune_prefix))
        self._call("command", "load_dump", location)
        return LoadResult(
            bytes=10,
            duration_ms=1.0,
            performed=self.performed,
            dropped=self.dropped if self.performed else (),
        )

    def drop_tables(
        self, location: DatabaseLocation, *, prefix: str
    ) -> tuple[str, ...]:
        assert prefix == "wp_"
        self._call("command", "drop_tables", location)
        return self.dropped

    def drop_database(self, location: DatabaseLocation, *, confirm: str) -> bool:
        assert confirm == location.name
        self._call("command", "drop_database", location)
        return True

    def ensure_needed(self) -> None:
        """``check_load`` answers that an administrator must provision first."""
        self.readiness["check_load"] = NEEDS_ADMIN


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


class DumpingSiteFiles:
    """The harness's site-files command; a Dump is extracted for real.

    The load and the rollback read the Dump they extract (the Safety
    Snapshot's included); every other extraction goes to ``inner``.
    """

    def __init__(self, log: CommandLog, inner: SiteFilesCommand) -> None:
        self.log = log
        self.inner = inner
        self.real = LocalSiteFiles()

    def clean(self, site_root: Path, *, keep: frozenset[PurePosixPath]) -> int:
        return self.inner.clean(site_root, keep=keep)

    def extract(
        self,
        source: StorageTarget,
        destination: Path,
        view: ArchiveView,
        *,
        roles: frozenset[MemberRole],
        exclude: frozenset[PurePosixPath] = frozenset(),
        payload: Path | None = None,
    ) -> ExtractResult:
        if roles != frozenset({MemberRole.DUMP}):
            return self.inner.extract(
                source, destination, view, roles=roles, exclude=exclude, payload=payload
            )
        self.log.add("site_files", "extract", (str(destination), ["dump"], []))
        return self.real.extract(
            source, destination, view, roles=roles, exclude=exclude, payload=payload
        )


@dataclass
class LoadHarness(Harness):
    """``Harness`` with the real load slots over :class:`FakeDatabase`.

    ``take_snapshot`` writes a real Safety Snapshot (an Archive holding a
    Dump), so a rollback reads it as the real Run would.
    """

    database: FakeDatabase = field(default_factory=FakeDatabase)
    failing_keys: frozenset[str] = frozenset()
    password: str | None = None
    #: The Run's ``Clock`` for ``load_live``; the system clock when ``None``.
    clock: Clock | None = None
    #: ``RestoreInputs.rollback`` in place of the plan's (``None``: derived).
    rollback: RollbackKind | None = None

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
            site_files_command=DumpingSiteFiles(self.log, base.site_files_command),
        )

    def ports(self, adapters: AdapterSet | None = None) -> RestorePorts:
        import dataclasses

        ports = super().ports(adapters)
        if self.password is None:
            return ports
        password = self.password
        return dataclasses.replace(ports, cutover_credentials=lambda: password)

    def header(self) -> JournalHeader:
        return load_header(
            run_id=RUN_ID,
            site_root=PurePosixPath(self.site_root),
            live=self.site.db_name,
            relocation=self.relocation,
            mode=self.mode,
            archive=str(self.archive_path),
            config_path=str(self.relocation.config_file or self.config_path),
            snapshot_path=str(self.snapshot_file),
            saved_config=super().header().saved_config,
        )

    def inputs(self) -> RestoreInputs:
        import dataclasses

        base = super().inputs()
        kind = self.rollback or database_rollback(
            self.site, self.relocation, self.target(), self.plan()
        )
        return dataclasses.replace(base, rollback=kind)

    def take_snapshot(self, scope: BackupScope) -> SnapshotResult:
        self.snapshots.append(scope)
        self.log.add("snapshot", "take", scope)
        self.snapshot_file.parent.mkdir(parents=True, exist_ok=True)
        if not self.snapshot_file.exists():
            build = self.work / "snapshot-build"
            build.mkdir()
            shutil.copyfile(write_v1(build), self.snapshot_file)
        return SnapshotResult(
            path=PurePosixPath(self.snapshot_file),
            scope=scope,
            encrypted=False,
            bytes=1,
            integrity=Integrity.VERIFIED,
            performed=True,
        )

    def run_load(
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
            functools.partial(check_live, ports=ports),
            functools.partial(
                load_live,
                ports=ports,
                workspace=self.workspace,
                cipher=self.cipher,
                **({} if self.clock is None else {"clock": self.clock}),
            ),
            journal or self.journal,
            self.cipher,
            survey_target=self.target,
            opted_out_by=self.opted_out_by,
            undo_relocation=functools.partial(
                roll_back_relocation,
                ports=ports,
                workspace=self.workspace,
                cipher=self.cipher,
            ),
        )


def load_header(
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
    saved_config: str | None = None,
) -> JournalHeader:
    """The header the restore root builds before ``SNAPSHOT`` (ADR-0021)."""
    load = load_database_name(live, relocation)
    prior_values = dict(prior) if prior is not None else dict(TARGET_DB)
    return JournalHeader(
        run_id=run_id,
        started_at="2026-11-03T00:00:00+00:00",
        site_root=site_root,
        restore_mode=mode,
        archive=archive,
        snapshot_path=snapshot_path,
        live_database_before=live,
        staging_database=None,
        wp_config_path=config_path,
        load_database=load,
        cutover_keys=planned_cutover_keys(relocation),
        cutover_targets=planned_cutover_targets(load, relocation),
        prior_constants={
            k: v for k, v in prior_values.items() if k in JOURNALED_CONSTANTS
        },
        phase_undo={},
        saved_config=saved_config,
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


def _live(header: JournalHeader) -> bool:
    """The Run loaded the live database itself: no relocation moved it."""
    return (
        header.staging_database is None
        and header.load_database == header.live_database_before
        and "DB_HOST" not in header.cutover_targets
    )


_INTERRUPTED = {
    STEP_LOAD_STARTED: "the load was interrupted",
    STEP_ROLLBACK_STARTED: "the load failed and its automatic rollback was interrupted",
    STEP_ROLLBACK_FAILED: "the load failed and its automatic rollback failed",
}


def _direct_row(step: str, header: JournalHeader, untouched: str) -> str:
    """The direct load's rows (owner decisions 49 and 50)."""
    load = header.load_database
    if step == STEP_DATABASE_CREATED:
        return f"{untouched}; this Run created database {load}"
    if step in _INTERRUPTED:
        what = _INTERRUPTED[step]
        if _live(header):
            return f"{what}, so the live database {load} is partly loaded"
        return f"{untouched}; {what}, so the relocated database {load} is partly"
    if step == STEP_LOADED:
        if header.cutover_keys:
            return (
                f"{untouched}; the Dump was loaded into {load} and checked, and "
                "the cutover had not started"
            )
        return f"the Dump was loaded into {load} and checked: the database is restored"
    if step == STEP_ROLLED_BACK:
        return f"the load failed and the database {load} was put back"
    assert step == STEP_LOAD_TARGET_DROPPED
    return f"{untouched}; the relocated database {load} this Run created was dropped"


_DIRECT = frozenset({*_INTERRUPTED, STEP_DATABASE_CREATED, STEP_LOADED})
_DIRECT_ALL = _DIRECT | {STEP_ROLLED_BACK, STEP_LOAD_TARGET_DROPPED}


def _load_row(step: str | None, header: JournalHeader, *, files_changed: bool) -> str:
    load, before = header.load_database, header.live_database_before
    keys = header.cutover_keys
    untouched = _files(header) if files_changed else "live site untouched"
    if step in _DIRECT_ALL:
        assert step is not None
        return _direct_row(step, header, untouched)
    if step is None and header.staging_database is None:
        return f"{untouched}; nothing was loaded yet"
    if step in {None, STEP_CHECK_FAILED, STEP_CUTOVER_READBACK_FAILED}:
        if header.staging_database is None:
            named = f"{load} was loaded by this Run; empty or drop it"
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

    Over every ``STEP_*`` constant ``core/restore_journal.py`` declares (the
    source of truth), each cutover step of ``header.cutover_keys``, no step and an
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
    "LoadHarness",
    "assert_one_row",
    "load_header",
    "undo_row",
    "undo_rows",
]
