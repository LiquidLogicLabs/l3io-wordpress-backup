"""A recording harness for ``execute_restore`` (E003-S01-001). Not a test module.

Every command half records into one ordered :class:`CommandLog`, tagged with
the journal phase active at the time, so a test asserts which parts a Mode
touched and which it did not. Query halves are real where the filesystem can
answer (``LocalSiteFiles``, ``TarArchiveProbe``) and in-memory otherwise. The
``load_database`` slot is a fake that journals the real slot's step names and
makes its cutover write through the recording config port.
"""

from __future__ import annotations

import io
import tarfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import IO, Any

from l3io.wp.backup.adapters.database import (
    locate_wp_config,
    site_descriptor_from_wp_config,
)
from l3io.wp.backup.adapters.noop import NoopTracer
from l3io.wp.backup.adapters.restore_runner import execute_restore, snapshot_result
from l3io.wp.backup.adapters.site_files import LocalSiteFiles
from l3io.wp.backup.adapters.tar import (
    TarArchiveProbe,
    collect_site_root,
    file_member,
    verify_archive_view,
    write_archive,
)
from l3io.wp.backup.cli import backup as backup_cli
from l3io.wp.backup.cli.context import RunContext
from l3io.wp.backup.core.restore import (
    CUTOVER_KEYS,
    NO_RELOCATION,
    STEP_CHECK_FAILED,
    STEP_CUTOVER,
    STEP_CUTOVER_STARTED,
    STEP_LOSER_DROP_STARTED,
    STEP_LOSER_DROPPED,
    STEP_STAGING_DROPPED,
    DatabaseRestoreReport,
    ExecutionReport,
    RestoreInputs,
    RestorePlan,
    RestoreRelocation,
    SnapshotResult,
    TargetState,
    build_restore_plan,
)
from l3io.wp.backup.domain.archive_format import (
    DUMP_RELATIVE,
    FORMAT_VERSION,
    PAYLOAD_MEMBER,
    ArchiveView,
    ExtractResult,
    Integrity,
    Manifest,
    ManifestMember,
    ManifestOrigin,
    ManifestPayload,
    MemberDigest,
    MemberRole,
    OriginKind,
)
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.config import ConfigWriteResult
from l3io.wp.backup.domain.database import DatabaseTargetState, EnsureOutcome
from l3io.wp.backup.domain.exclusion import INCLUDE_EVERYTHING
from l3io.wp.backup.domain.journal import JournalHeader
from l3io.wp.backup.domain.restore import (
    ConfigPolicy,
    Override,
    OverrideSet,
    RestoreMode,
    RestorePhase,
    SnapshotPolicy,
    removes_database,
)
from l3io.wp.backup.domain.search_replace import SearchReplaceReport
from l3io.wp.backup.domain.site import SiteDescriptor
from l3io.wp.backup.domain.storage_target import StorageTarget
from l3io.wp.backup.ports.adapter_set import AdapterSet
from l3io.wp.backup.ports.journal import RunJournalCommand
from l3io.wp.backup.ports.restore import RestorePorts, restore_ports

#: The target's own ``DB_*`` values, as its ``wp-config.php`` holds them.
TARGET_DB = {
    "DB_HOST": "db.target",
    "DB_USER": "target_user",
    "DB_PASSWORD": "target-password-0",
    "DB_NAME": "target_db",
}
#: The Archive's ``DB_*`` values, which must never reach the target.
ARCHIVE_DB = {
    "DB_HOST": "db.source",
    "DB_USER": "source_user",
    "DB_PASSWORD": "source-password-0",
    "DB_NAME": "source_db",
}
INDEX = b"<?php // restored index\n"
DUMP = b"CREATE TABLE wp_users (id INT);\n"


def wp_config(values: Mapping[str, str]) -> str:
    lines = [f"define('{k}', '{v}');" for k, v in values.items()]
    return "<?php\n" + "\n".join(lines) + "\n$table_prefix = 'wp_';\n"


class IdentityCipher:
    """A ``PayloadCipher`` that copies bytes; ``decrypt`` raises ``failure``."""

    def __init__(self, failure: BaseException | None = None) -> None:
        self.failure = failure
        self.checked = 0

    def check(self) -> None:
        self.checked += 1

    def encrypt(self, reader: IO[bytes], writer: IO[bytes]) -> None:
        writer.write(reader.read())

    def decrypt(self, reader: IO[bytes], writer: IO[bytes]) -> None:
        if self.failure is not None:
            raise self.failure
        writer.write(reader.read())


def write_v1(
    work: Path,
    *,
    config: bool = True,
    dump: bool = True,
    cipher: IdentityCipher | None = None,
    config_role: bool = False,
) -> Path:
    """A small v1 Archive by the real writer: index, config, Dump.

    ``config_role`` stores the config as a ``config``-role member (a config
    one level up, FR-29) instead of in the Site Root tree.
    """
    source = work / "source-site"
    source.mkdir()
    (source / "index.php").write_bytes(INDEX)
    config_file = (work if config_role else source) / "wp-config.php"
    if config:
        config_file.write_text(wp_config(ARCHIVE_DB), encoding="utf-8")
    members = list(collect_site_root(source, INCLUDE_EVERYTHING).members)
    if config and config_role:
        members.append(file_member(MemberRole.CONFIG, "wp-config.php", config_file))
    if dump:
        dump_file = work / "dump.sql"
        dump_file.write_bytes(DUMP)
        members.insert(0, file_member(MemberRole.DUMP, DUMP_RELATIVE, dump_file))

    def manifest(index: tuple[ManifestMember, ...]) -> Manifest:
        return Manifest(
            format_version=FORMAT_VERSION,
            created_at="2026-11-03T00:00:00+00:00",
            backup_scope="all" if dump else "files",
            source={"db_name": "source_db"},
            encrypted=False,
            payload=ManifestPayload(name=PAYLOAD_MEMBER, size=0, sha256="0" * 64),
            members=index,
            dump_present=dump,
            config_path=(
                None
                if not config
                else "../wp-config.php"
                if config_role
                else "wp-config.php"
            ),
        )

    workspace = work / "write-workspace"
    workspace.mkdir(mode=0o700)
    path = work / "archive.tar.gz"
    with path.open("wb") as out:
        write_archive(out, manifest, members, workspace=workspace, cipher=cipher)
    return path


def write_v0(work: Path, *, index: bytes = INDEX) -> Path:
    """A v0 Archive: ``wp-root/`` and ``database.sql``, no Manifest."""
    path = work / "v0.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        root = tarfile.TarInfo("wp-root")
        root.type = tarfile.DIRTYPE
        root.mode = 0o755
        tar.addfile(root)
        for name, data in (("wp-root/index.php", index), ("database.sql", DUMP)):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return path


@dataclass
class CommandLog:
    """Every command call, in order, with the journal phase active at the time."""

    calls: list[tuple[RestorePhase | None, str, str, Any]] = field(default_factory=list)
    phase: RestorePhase | None = None

    def add(self, port: str, method: str, detail: object = None) -> None:
        self.calls.append((self.phase, port, method, detail))

    def methods(self) -> list[str]:
        return [f"{port}.{method}" for _, port, method, _ in self.calls]


class RecordingJournal:
    """``RunJournalCommand``: records every call; sets the log's phase."""

    def __init__(self, log: CommandLog) -> None:
        self.log = log
        self.events: list[tuple[str, Any]] = []

    def begin_run(self, header: JournalHeader) -> None:
        self.events.append(("begin_run", header.run_id))

    def phase_started(self, phase: RestorePhase, target: str) -> None:
        self.log.phase = phase
        self.events.append(("phase_started", (phase, target)))

    def step_completed(self, phase: RestorePhase, step: str) -> None:
        self.events.append(("step_completed", (phase, step)))

    def phase_completed(self, phase: RestorePhase) -> None:
        self.events.append(("phase_completed", phase))

    def finish_run(self) -> None:
        self.events.append(("finish_run", None))

    def resolve(self, run_id: str, resolved_by: str) -> None:
        self.events.append(("resolve", run_id))

    def names(self) -> list[str]:
        return [name for name, _ in self.events]


class RecordingSiteFiles:
    """``SiteFilesCommand``: records; delegates to ``real`` when given."""

    def __init__(self, log: CommandLog, real: LocalSiteFiles | None = None) -> None:
        self.log = log
        self.real = real

    def clean(self, site_root: Path, *, keep: frozenset[PurePosixPath]) -> int:
        self.log.add("site_files", "clean", sorted(str(k) for k in keep))
        return 0 if self.real is None else self.real.clean(site_root, keep=keep)

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
        detail = (
            str(destination),
            sorted(r.value for r in roles),
            sorted(map(str, exclude)),
        )
        self.log.add("site_files", "extract", detail)
        if self.real is None:
            return ExtractResult(members=1, bytes=len(INDEX), skipped_links=())
        return self.real.extract(
            source, destination, view, roles=roles, exclude=exclude, payload=payload
        )


class FakeConfig:
    """Both config halves over in-memory constants; records every write."""

    def __init__(self, log: CommandLog, values: Mapping[str, str]) -> None:
        self.log = log
        self.values: dict[str, str | None] = dict(values)

    def read_constants(
        self, config_file: Path, keys: Sequence[str]
    ) -> Mapping[str, Any]:
        return {k: self.values.get(k) for k in keys}

    def set_constants(
        self, config_file: Path, values: Mapping[str, str | int | bool]
    ) -> ConfigWriteResult:
        self.log.add("config", "set_constants", dict(values))
        self.values.update({k: str(v) for k, v in values.items()})
        return ConfigWriteResult(changed=tuple(values), performed=True)

    def save_copy(self, config_file: Path, destination: Path) -> ConfigWriteResult:
        self.log.add("config", "save_copy", str(destination))
        return ConfigWriteResult(changed=(), performed=True)

    def replace_file(
        self, config_file: Path, content: str, *, keep: Mapping[str, Any]
    ) -> ConfigWriteResult:
        self.log.add("config", "replace_file", dict(keep))
        self.values = {**ARCHIVE_DB, **keep}
        return ConfigWriteResult(changed=tuple(keep), performed=True)


class RecordingPort:
    """Any other command half: every call is recorded and returns ``None``."""

    def __init__(self, log: CommandLog, port: str) -> None:
        self._log = log
        self._port = port

    def __getattr__(self, method: str) -> Callable[..., None]:
        def call(*args: object, **kwargs: object) -> None:
            self._log.add(self._port, method, (args, kwargs))

        return call


class FakeSearchReplace:
    def __init__(self, log: CommandLog) -> None:
        self.log = log

    def relocate_content(self, site_root: Path, urls: object) -> SearchReplaceReport:
        self.log.add("search_replace", "relocate_content", urls)
        return SearchReplaceReport(
            pairs=(), replacements=0, tables=(), remaining=0, merged=True
        )


class Unused:
    """A port the restore must never reach."""

    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, method: str) -> object:
        msg = f"the restore reached {self._name}.{method}"
        raise AssertionError(msg)


class SpyProbe(TarArchiveProbe):
    """The real probe, counting ``member_digests`` reads."""

    __slots__ = ("digest_reads",)

    def __init__(self) -> None:
        self.digest_reads = 0

    def member_digests(self, source: StorageTarget) -> Mapping[str, MemberDigest]:
        self.digest_reads += 1
        return super().member_digests(source)


@dataclass
class Harness:
    """Everything one ``execute_restore`` call needs, and what it recorded."""

    work: Path
    archive_path: Path
    mode: RestoreMode = RestoreMode.ALL
    relocation: RestoreRelocation = NO_RELOCATION
    snapshot: SnapshotPolicy = SnapshotPolicy.TAKE
    config_policy: ConfigPolicy = ConfigPolicy.KEEP
    cipher: IdentityCipher | None = None
    free: int = 10**12
    real_files: bool = False
    slot_failure: tuple[str, ...] | None = None
    check_failure: BaseException | None = None
    log: CommandLog = field(default_factory=CommandLog)
    preflights: list[tuple[Any, ...]] = field(default_factory=list)
    checks: int = 0
    snapshots: list[BackupScope] = field(default_factory=list)
    slot_calls: int = 0
    view_override: ArchiveView | None = None
    #: The restore root's step-6 survey fact (E003-S02-004); :meth:`target`'s
    #: database exists, so ``True`` unless a test removes both.
    live_database_exists: bool = True
    #: The FR-7 waivers: ``--force`` by default, since the target is populated.
    overrides: OverrideSet = field(
        default_factory=lambda: OverrideSet(waived=frozenset(Override))
    )

    def __post_init__(self) -> None:
        self.site_root = self.work / "target"
        self.site_root.mkdir(exist_ok=True)
        (self.site_root / "old.php").write_bytes(b"<?php // old\n")
        self.config_path = self.site_root / "wp-config.php"
        self.config_path.write_text(wp_config(TARGET_DB), encoding="utf-8")
        self.workspace = self.work / "workspace"
        self.workspace.mkdir(exist_ok=True, mode=0o700)
        self.journal = RecordingJournal(self.log)
        self.config = FakeConfig(self.log, TARGET_DB)
        self.probe = SpyProbe()
        self.archive = StorageTarget(
            scheme="file", path=PurePosixPath(self.archive_path)
        )
        verified, _ = verify_archive_view(self.archive_path)
        assert verified is not None
        self.view = self.view_override or verified
        self.site = SiteDescriptor(
            site_root=PurePosixPath(self.site_root),
            db_host="db.target",
            db_port=3306,
            db_name="target_db",
            table_prefix="wp_",
            site_url=None,
            home_url=None,
            credential_source=f"wp-config:{self.config_path}",
        )

    def adapters(self) -> AdapterSet:
        real = LocalSiteFiles() if self.real_files else None
        return AdapterSet(
            archive_store_query=Unused("archive_store_query"),  # type: ignore[arg-type]
            archive_store_command=Unused("archive_store_command"),  # type: ignore[arg-type]
            archive_probe=self.probe,
            database_query=Unused("database_query"),  # type: ignore[arg-type]
            database_command=RecordingPort(self.log, "database"),  # type: ignore[arg-type]
            archive_writer=Unused("archive_writer"),  # type: ignore[arg-type]
            tracer=NoopTracer(),
            restore_database_query=Unused("restore_database_query"),  # type: ignore[arg-type]
            restore_database_command=RecordingPort(self.log, "restore_database"),  # type: ignore[arg-type]
            site_files_query=LocalSiteFiles(),
            site_files_command=RecordingSiteFiles(self.log, real),
            config_query=self.config,
            config_command=self.config,
            search_replace=FakeSearchReplace(self.log),
            journal_query=Unused("journal_query"),  # type: ignore[arg-type]
            journal_command=self.journal,
        )

    def ports(self, adapters: AdapterSet | None = None) -> RestorePorts:
        def preflight(tools: tuple[Any, ...]) -> None:
            self.preflights.append(tools)

        return restore_ports(
            adapters or self.adapters(),
            preflight=preflight,
            free_bytes=lambda _path: self.free,
            cutover_credentials=None,
        )

    def target(self) -> TargetState:
        entries = len(list(self.site_root.iterdir()))
        return TargetState(
            site_root_empty=entries == 0,
            database=DatabaseTargetState(True, 12, True, "57155", True, 0, 0),
            config_exists=self.config_path.exists(),
            site_root=self.site.site_root,
            site_root_entries=entries,
            database_location=self.site.database_location(),
            load_target=None,
            load_location=self.relocation.load_location,
        )

    def plan(self) -> RestorePlan:
        return build_restore_plan(
            self.site,
            self.mode,
            self.overrides,
            self.view,
            self.target(),
            archive=self.archive,
            relocation=self.relocation,
            snapshot=self.snapshot,
            snapshot_dir=self.work / "snapshots",
            config_dir=self.site_root,
            config_policy=self.config_policy,
        )

    def header(self) -> JournalHeader:
        return JournalHeader(
            run_id="run-1",
            started_at="2026-11-03T00:00:00+00:00",
            site_root=PurePosixPath(self.site_root),
            restore_mode=self.mode,
            archive=str(self.archive_path),
            snapshot_path=str(self.snapshot_file),
            live_database_before="target_db",
            staging_database="target_db_staging",
            wp_config_path=str(self.config_path),
            load_database="target_db_staging",
            cutover_keys=("DB_NAME",),
            cutover_targets={"DB_NAME": "target_db_staging"},
            prior_constants={"DB_NAME": "target_db"},
            phase_undo={},
            saved_config=str(self.work / "saved-wp-config.php"),
        )

    def inputs(self) -> RestoreInputs:
        return RestoreInputs(
            site=self.site,
            view=self.view,
            archive=self.archive,
            run_id="run-1",
            relocation=self.relocation,
            header=self.header(),
            config_policy=self.config_policy,
            config_path=self.config_path,
            payload=self.workspace / PAYLOAD_MEMBER if self.view.encrypted else None,
            workspace=self.workspace,
            live_database_exists=self.live_database_exists,
        )

    @property
    def snapshot_file(self) -> Path:
        """The Safety Snapshot path the header plans and the fake reports."""
        return self.work / "snapshots" / "s.tar.gz"

    @property
    def opted_out_by(self) -> str | None:
        return "--no-safety-snapshot" if self.snapshot is SnapshotPolicy.SKIP else None

    def take_snapshot(self, scope: BackupScope) -> SnapshotResult:
        self.snapshots.append(scope)
        self.log.add("snapshot", "take", scope)
        return SnapshotResult(
            path=PurePosixPath(self.snapshot_file),
            scope=scope,
            encrypted=False,
            bytes=1,
            integrity=Integrity.VERIFIED,
            performed=True,
        )

    def check_database(self, inputs: RestoreInputs) -> None:
        self.checks += 1
        if self.check_failure is not None:
            raise self.check_failure

    def load_database(
        self, inputs: RestoreInputs, journal: RunJournalCommand
    ) -> DatabaseRestoreReport:
        """The fake slot: the real slot's step names, the cutover through config."""
        self.slot_calls += 1
        self.log.add("slot", "load", inputs.header.restore_mode)
        if self.slot_failure is not None:
            for step in self.slot_failure:
                journal.step_completed(RestorePhase.LOAD_DUMP, step)
            msg = "the fake slot failed"
            raise RuntimeError(msg)
        journal.step_completed(RestorePhase.LOAD_DUMP, STEP_CUTOVER_STARTED + "DB_NAME")
        cutover: dict[str, str | int | bool] = {"DB_NAME": "target_db_staging"}
        rel = inputs.relocation
        if rel.cutover_host is not None:
            cutover["DB_HOST"] = rel.cutover_host
        if rel.cutover_user is not None:
            cutover["DB_USER"] = rel.cutover_user
        self.config.set_constants(Path(inputs.config_path), cutover)
        journal.step_completed(RestorePhase.LOAD_DUMP, STEP_CUTOVER + "DB_NAME")
        dropped = removes_database(inputs.header.restore_mode)
        if dropped:
            journal.step_completed(RestorePhase.LOAD_DUMP, STEP_LOSER_DROP_STARTED)
            self.log.add("restore_database", "drop_database", "target_db")
            journal.step_completed(RestorePhase.LOAD_DUMP, STEP_LOSER_DROPPED)
        return DatabaseRestoreReport(
            live_database_before="target_db",
            live_database_after="target_db_staging",
            ensure=EnsureOutcome(False, False, False, False, (), True),
            loaded_bytes=len(DUMP),
            load_duration_ms=1.0,
            previous_database="dropped" if dropped else "retained",
            restored=True,
            performed=True,
        )

    def run(
        self,
        *,
        plan: RestorePlan | None = None,
        adapters: AdapterSet | None = None,
        journal: RunJournalCommand | None = None,
    ) -> ExecutionReport:
        return execute_restore(
            plan or self.plan(),
            self.inputs(),
            self.ports(adapters),
            self.take_snapshot,
            self.check_database,
            self.load_database,
            journal or self.journal,
            self.cipher,
            survey_target=self.target,
            opted_out_by=self.opted_out_by,
        )


ORDERLY_CHECK_FAILURE = (STEP_CHECK_FAILED, STEP_STAGING_DROPPED)
NON_ORDERLY_FAILURE = (STEP_CUTOVER_STARTED + "DB_NAME", STEP_CUTOVER + "DB_NAME")

__all__ = [
    "ARCHIVE_DB",
    "CUTOVER_KEYS",
    "NON_ORDERLY_FAILURE",
    "ORDERLY_CHECK_FAILURE",
    "TARGET_DB",
    "CommandLog",
    "Harness",
    "IdentityCipher",
    "joined_snapshot",
    "write_v0",
    "write_v1",
]


def joined_snapshot(
    harness: Harness, ctx: RunContext
) -> Callable[[BackupScope], SnapshotResult]:
    """``take_snapshot`` as the root binds it: join ``backup``, then check it."""
    root = harness.site_root.resolve()
    config = locate_wp_config(root, stated=None)
    site, _ = site_descriptor_from_wp_config(root, config_file=config.path)

    def take(scope: BackupScope) -> SnapshotResult:
        result = backup_cli.backup(
            site,
            scope,
            backup_cli.storage_target_from_cli(str(harness.snapshot_file)),
            ctx=ctx,
            config=config,
            cipher=None,
            exclusion=INCLUDE_EVERYTHING,
            origin=ManifestOrigin(
                kind=OriginKind.SAFETY_SNAPSHOT,
                restore_mode=harness.mode.value,
                run_id=ctx.run_id,
            ),
        )
        return snapshot_result(
            PurePosixPath(result.path),
            scope=result.scope,
            encrypted=result.encrypted,
            size=result.bytes,
            performed=result.performed,
        )

    return take
