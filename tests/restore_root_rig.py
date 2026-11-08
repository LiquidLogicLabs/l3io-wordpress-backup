# ruff: noqa: ANN401 - recorders forward whatever the root passes them
"""Recording doubles for the restore root's unit tests (E003-S02-006).

Not a test module. :class:`Rig` drives the real :func:`drive_restore` over a
fixture Site and a real v1 Archive, with:

- ``real_adapters`` replaced by a recorder that builds the real set and swaps
  every database half for :class:`FakeDatabase` (no server), so ``bind_adapters``
  and ``dry_run_adapters`` run unchanged;
- ``execute_restore`` replaced by a recorder that keeps its arguments and
  returns a report built from the plan, unless a test sets ``execute`` to
  call the real executor;
- ``build_restore_plan``, ``backup`` and ``FileRunJournal.incomplete`` wrapped
  so each call is appended to one ordered ``events`` list.

Every name it patches is looked up through ``cli/restore.py``'s module, the
one the root resolves it from.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from l3io.wp.backup.adapters import journal as journal_module
from l3io.wp.backup.adapters.database import AdminTls
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.adapters.restore_runner import execute_restore
from l3io.wp.backup.cli import restore as restore_mod
from l3io.wp.backup.cli.backup import BackupResult
from l3io.wp.backup.core.restore import (
    DatabaseRestoreReport,
    ExecutionReport,
    RestoreInputs,
    RestorePlan,
    SnapshotResult,
)
from l3io.wp.backup.domain.archive_format import Integrity
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.database import (
    DatabaseLocation,
    DatabaseTargetState,
    EnsureOutcome,
    Readiness,
)
from l3io.wp.backup.domain.restore import (
    ConfigPolicy,
    Override,
    OverrideSet,
    RestoreMode,
    RestorePhase,
    SnapshotPolicy,
)
from l3io.wp.backup.domain.storage_target import StorageTarget
from l3io.wp.backup.ports.adapter_set import AdapterSet
from tests.restore_harness import TARGET_DB, wp_config, write_v1
from tests.root_mode import running_as_root

#: Every waiver: a test about something else is not refused by FR-7.
ALL_WAIVED = OverrideSet(waived=frozenset(Override))


class FakeDatabase:
    """Both database halves of one account, recorded; never a server."""

    def __init__(self, events: list[object], state: DatabaseTargetState) -> None:
        self.events = events
        self.state = state
        self.commands: list[str] = []
        self.administrator_bound = True

    def survey_target(
        self,
        location: DatabaseLocation,
        *,
        table_prefix: str,
        wordpress_user: str | None,
    ) -> DatabaseTargetState:
        self.events.append(("survey", location.name, wordpress_user))
        return self.state

    def check_dump(self, location: DatabaseLocation) -> Readiness:
        self.events.append(("check_dump", location.name))
        return Readiness(ready=True)

    def __getattr__(self, name: str) -> Callable[..., object]:
        if name.startswith("check_"):
            return lambda *a, **k: Readiness(ready=True)

        def command(*args: object, **kwargs: object) -> object:
            self.commands.append(name)
            self.events.append(("db_command", name))
            return None

        return command


def report_for(
    plan: RestorePlan, *, restored: bool = True, performed: bool = True
) -> ExecutionReport:
    """The report a completed executor returns for ``plan``."""
    database = None
    if RestorePhase.LOAD_DUMP in plan.phases:
        database = DatabaseRestoreReport(
            live_database_before="target_db",
            live_database_after="target_db_l3s_x",
            ensure=EnsureOutcome(False, False, False, False, (), performed),
            loaded_bytes=1,
            load_duration_ms=1.0,
            previous_database="retained",
            restored=restored,
            performed=performed,
        )
    return ExecutionReport(
        phases=plan.phases,
        snapshot=None,
        database=database,
        search_replace=None,
        integrity=Integrity.VERIFIED,
        skipped_links=(),
        wp_config_kept=False,
    )


@dataclass
class Rig:
    """One fixture Site, one Archive, and every recorder the root is driven over."""

    tmp: Path
    monkeypatch: pytest.MonkeyPatch
    events: list[object] = field(default_factory=list)
    adapter_calls: list[dict[str, Any]] = field(default_factory=list)
    plans: list[RestorePlan] = field(default_factory=list)
    plan_calls: list[dict[str, Any]] = field(default_factory=list)
    executions: list[dict[str, Any]] = field(default_factory=list)
    backups: list[dict[str, Any]] = field(default_factory=list)
    snapshot_performed: bool = True
    survey: DatabaseTargetState = field(
        default_factory=lambda: DatabaseTargetState(True, 12, True, "57155", True, 0, 0)
    )
    execute: Callable[..., ExecutionReport] | None = None
    database: FakeDatabase | None = None

    def __post_init__(self) -> None:
        self.work = self.tmp / "rig"
        (self.work / "archive").mkdir(parents=True)
        self.archive_path = write_v1(self.work / "archive")
        self.site_root = self.work / "site"
        self.site_root.mkdir()
        (self.site_root / "index.php").write_bytes(b"<?php // old\n")
        self.config = self.site_root / "wp-config.php"
        self.config.write_text(wp_config(TARGET_DB), encoding="utf-8")
        self._patch()

    # -- the recorders ------------------------------------------------------

    def _patch(self) -> None:
        mp = self.monkeypatch
        real_adapters = restore_mod.real_adapters
        real_plan = restore_mod.build_restore_plan
        real_incomplete = journal_module.FileRunJournal.incomplete

        def adapters(**kwargs: Any) -> AdapterSet:
            self.events.append("real_adapters")
            self.adapter_calls.append(kwargs)
            real = real_adapters(**kwargs)
            self.database = FakeDatabase(self.events, self.survey)
            return dataclasses.replace(
                real,
                database_query=self.database,  # type: ignore[arg-type]
                database_command=self.database,  # type: ignore[arg-type]
                restore_database_query=self.database,  # type: ignore[arg-type]
                restore_database_command=self.database,  # type: ignore[arg-type]
            )

        def plan(*args: Any, **kwargs: Any) -> RestorePlan:
            self.events.append("build_restore_plan")
            self.plan_calls.append({"args": args, **kwargs})
            built = real_plan(*args, **kwargs)
            self.plans.append(built)
            return built

        def incomplete(journal: Any, site_root: PurePosixPath) -> Any:
            self.events.append("journal.incomplete")
            return real_incomplete(journal, site_root)

        def execute(*args: Any, **kwargs: Any) -> ExecutionReport:
            self.events.append("execute_restore")
            names = (
                "plan",
                "inputs",
                "ports",
                "take_snapshot",
                "check_database",
                "load_database",
                "journal",
                "cipher",
            )
            call = dict(zip(names, args, strict=True)) | kwargs
            self.executions.append(call)
            if self.execute is not None:
                return self.execute(*args, **kwargs)
            return report_for(call["plan"])

        def backup(*args: Any, **kwargs: Any) -> BackupResult:
            self.events.append("backup")
            self.backups.append({"args": args, **kwargs})
            target: StorageTarget = args[2]
            return BackupResult(
                path=target.path,
                scope=args[1],
                bytes=0,
                encrypted=False,
                integrity=None,
                performed=self.snapshot_performed,
                plan=None,  # type: ignore[arg-type]
            )

        mp.setattr(restore_mod, "real_adapters", adapters)
        mp.setattr(restore_mod, "build_restore_plan", plan)
        mp.setattr(restore_mod, "execute_restore", execute)
        mp.setattr(restore_mod, "backup", backup)

        def published(path: Any, **kwargs: Any) -> SnapshotResult:
            # The post-publish verification reads a real file; the rig has none.
            performed = kwargs["performed"]
            return SnapshotResult(
                path=path,
                scope=kwargs["scope"],
                encrypted=kwargs["encrypted"],
                bytes=1 if performed else 0,
                integrity=Integrity.VERIFIED if performed else None,
                performed=performed,
            )

        mp.setattr(restore_mod, "snapshot_result", published)
        mp.setattr(journal_module.FileRunJournal, "incomplete", incomplete)

    def real_executor(self) -> None:
        """Let the recorded ``execute_restore`` call the real executor."""
        self.execute = execute_restore

    # -- requests -----------------------------------------------------------

    def request(self, **changes: Any) -> restore_mod.RestoreRequest:
        values: dict[str, Any] = {
            "site_root": self.site_root,
            "archive": StorageTarget(
                scheme="file", path=PurePosixPath(self.archive_path)
            ),
            "mode": RestoreMode.ALL,
            "overrides": ALL_WAIVED,
            "snapshot": SnapshotPolicy.TAKE,
            "snapshot_dir": self.work / "snapshots",
            "config_policy": ConfigPolicy.KEEP,
            "resolve_run_id": None,
            "admin": None,
            "cipher": None,
            "archive_database": None,
            "admin_tls": AdminTls(mode=None, ca_file=None),
            "relocation": restore_mod.RelocationFlags.none(),
            "reset_existing_db_user": False,
            "dry_run": False,
            # CI runs as root; the refusal is proved where it is asked for.
            "allow_root": running_as_root(),
        }
        values.update(changes)
        return restore_mod.RestoreRequest(**values)

    def drive(self, **changes: Any) -> restore_mod.RestoreOutcome:
        return restore_mod.drive_restore(
            self.request(**changes), ctx=None, secrets=SecretRegistry()
        )

    @property
    def inputs(self) -> RestoreInputs:
        inputs: RestoreInputs = self.executions[-1]["inputs"]
        return inputs

    def take_snapshot(self, scope: BackupScope = BackupScope.FILES) -> SnapshotResult:
        taker: Callable[[BackupScope], SnapshotResult] = self.executions[-1][
            "take_snapshot"
        ]
        return taker(scope)


__all__ = ["ALL_WAIVED", "FakeDatabase", "Rig", "report_for"]
