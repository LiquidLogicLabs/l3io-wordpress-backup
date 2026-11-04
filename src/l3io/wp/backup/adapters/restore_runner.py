"""AD-4, AD-11, AD-17, AD-18, ADR-0021, ADR-0022 — the restore executor.

:func:`execute_restore` walks ``plan.phases`` in order — every phase,
``PREFLIGHT`` and ``VERIFY`` included — inside ``adapters/phase.py``'s
:func:`~l3io.wp.backup.adapters.phase.phase`, so each emits one
``phase_completed`` record. Dispatch is one ``match`` over every
:class:`~l3io.wp.backup.domain.restore.RestorePhase` ending in
``assert_never``.

* ``PREFLIGHT`` makes queries only, in this order: the tool preflight, the
  cipher's ``check()``, :func:`~l3io.wp.backup.adapters.tar.require_patched_tarfile`,
  the BL-E002-116 size and free-space check, and ``check_database`` when the
  plan loads a Dump.
* ``VERIFY`` verifies the Archive (a failure raises before any command),
  refuses a view that changed since the root's probe, and decrypts an
  encrypted payload into the Run workspace.
* ``SNAPSHOT`` (FR-8, AD-12) calls the root's ``take_snapshot`` slot, which
  joins E002's ``backup(..., ctx=ctx)`` and builds its result with
  :func:`snapshot_result`. Any failure becomes
  :class:`~l3io.wp.backup.core.restore.SnapshotFailedError`, and a result
  that does not prove a verified snapshot at the planned path is refused
  (ADV-5): a not-performed snapshot is accepted only when every command
  half a destroying phase uses is a no-op, so nothing can be destroyed
  without a copy.
* Immediately before the first destroying phase the target is surveyed again
  through the root's ``survey_target`` slot and
  :func:`~l3io.wp.backup.core.restore.recheck_target` refuses a target that
  outgrew the plan (RT-003, ADV-4).
* The Run Journal begins immediately before the first phase after
  ``VERIFY``. It finishes only after the last phase succeeds, or after a
  failure :func:`~l3io.wp.backup.core.restore.is_orderly_failure` accepts for
  the entries this Run journaled, which are kept in memory as they are
  written, so a Dry Run (whose no-op journal persists nothing) evaluates the
  same entries. There is no ``finally: finish_run()``.

A Dry Run is the composition root binding no-op command halves (AD-4); this
module has no branch on it. ``take_snapshot``, ``check_database`` and
``load_database`` are slots the root fills, so the executor imports neither
the Run Context nor ``adapters/staging.py``.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import ClassVar, Final, assert_never

import structlog

from l3io.wp.backup.adapters import noop
from l3io.wp.backup.adapters.phase import PhaseRecord, phase
from l3io.wp.backup.adapters.tar import (
    decrypt_payload,
    require_patched_tarfile,
    verify_archive,
    verify_archive_view,
)
from l3io.wp.backup.core.restore import (
    CONFIG_FILE_NAME,
    CUTOVER_KEYS,
    SNAPSHOT_FILESYSTEM,
    STEP_CONFIG,
    STEP_CONFIG_REPLACED,
    STEP_CONFIG_SAVED,
    STEP_CONFIG_STARTED,
    TARGET_FILESYSTEM,
    WORKSPACE_FILESYSTEM,
    ArchiveHasNoConfigError,
    DatabaseRestoreReport,
    ExecutionReport,
    RestoreInputs,
    RestorePlan,
    SnapshotFailedError,
    SnapshotResult,
    SpaceClaim,
    TargetState,
    check_claims,
    check_expansion,
    is_orderly_failure,
    recheck_target,
    space_need,
)
from l3io.wp.backup.domain.archive_format import (
    ArchiveView,
    Integrity,
    MemberKind,
    MemberRole,
    SkippedLink,
    ViewMember,
    format_label,
    member_path,
    member_relative,
)
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.errors import (
    ArchiveNotFoundError,
    ArchiveWriteError,
    BackupError,
    RefusedError,
    UnsafeMemberPathError,
    VerificationFailedError,
)
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.domain.journal import JournalEntry, JournalEvent, JournalHeader
from l3io.wp.backup.domain.restore import (
    RESTORE_PHASE_ORDER,
    ConfigPolicy,
    RestorePhase,
    SnapshotPolicy,
    restores_database,
    restores_files,
)
from l3io.wp.backup.domain.search_replace import SearchReplaceReport
from l3io.wp.backup.ports.adapter_set import AdapterSet
from l3io.wp.backup.ports.crypto import PayloadCipher
from l3io.wp.backup.ports.journal import RunJournalCommand
from l3io.wp.backup.ports.restore import RestorePorts

__all__ = [
    "ArchiveChangedError",
    "TargetConfigMissingError",
    "execute_restore",
    "snapshot_result",
]

_log = structlog.get_logger(__name__)

#: The phases that run before the journal begins: queries and verification.
_CHECK_PHASES: Final = frozenset({RestorePhase.PREFLIGHT, RestorePhase.VERIFY})
#: The largest Archive ``wp-config.php`` ``--overwrite-wp-config`` reads.
_CONFIG_MAX_BYTES: Final = 1024 * 1024
#: Where the Archive's config is copied in the Run workspace (``0600``).
_ARCHIVE_CONFIG: Final = "archive-wp-config.php"
_ARCHIVE_CONFIG_DIR: Final = "archive-config"
_CONFIG_MODE: Final = 0o600
#: The relative path ``CLEAN`` keeps and ``EXTRACT`` excludes under ``KEEP``.
_KEPT_CONFIG: Final = frozenset({PurePosixPath(CONFIG_FILE_NAME)})
#: The phases that change the Site — every phase AD-17 orders after
#: ``SNAPSHOT``, derived so a new phase is never missed; the target is
#: re-surveyed before the first of them.
_DESTROYING: Final = frozenset(
    RESTORE_PHASE_ORDER[RESTORE_PHASE_ORDER.index(RestorePhase.SNAPSHOT) + 1 :]
)
#: The ``AdapterSet`` command halves the destroying phases reach.
_DESTROYING_HALVES: Final = (
    "site_files_command",
    "config_command",
    "restore_database_command",
    "search_replace",
)


class ArchiveChangedError(BackupError):
    """The Archive verified now is not the one the restore was planned from."""

    exit_code: ClassVar[ExitCode] = ExitCode.VERIFICATION_FAILED

    def __init__(self, archive: str) -> None:
        self.archive = archive
        super().__init__(
            f"archive {archive!r} changed between planning and verification; "
            "nothing was changed"
        )


class TargetConfigMissingError(RefusedError):
    """The plan writes the target's ``wp-config.php``, and none is there now."""

    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(
            f"the plan rewrites {path!r}, which does not exist (the target changed "
            "after planning); nothing was changed"
        )


class _RecordingJournal:
    """``RunJournalCommand`` over ``inner``, keeping this Run's entries in memory."""

    __slots__ = ("_inner", "entries")

    def __init__(self, inner: RunJournalCommand) -> None:
        self._inner = inner
        self.entries: list[JournalEntry] = []

    def _keep(
        self,
        event: JournalEvent,
        phase_: RestorePhase,
        target: str | None,
        step: str | None,
    ) -> None:
        at = datetime.now(UTC).isoformat()
        self.entries.append(
            JournalEntry(event=event, phase=phase_, at=at, target=target, step=step)
        )

    def begin_run(self, header: JournalHeader) -> None:
        self._inner.begin_run(header)

    def phase_started(self, phase: RestorePhase, target: str) -> None:
        self._inner.phase_started(phase, target)
        self._keep("phase_started", phase, target, None)

    def step_completed(self, phase: RestorePhase, step: str) -> None:
        self._inner.step_completed(phase, step)
        self._keep("step_completed", phase, None, step)

    def phase_completed(self, phase: RestorePhase) -> None:
        self._inner.phase_completed(phase)
        self._keep("phase_completed", phase, None, None)

    def finish_run(self) -> None:
        self._inner.finish_run()

    def resolve(self, run_id: str, resolved_by: str) -> None:
        self._inner.resolve(run_id, resolved_by)


@dataclass(slots=True)
class _Run:
    """One execution: its inputs, its slots and what the phases produced."""

    plan: RestorePlan
    inputs: RestoreInputs
    ports: RestorePorts
    journal: _RecordingJournal
    cipher: PayloadCipher | None
    take_snapshot: Callable[[BackupScope], SnapshotResult]
    check_database: Callable[[RestoreInputs], None]
    load_database: Callable[[RestoreInputs, RunJournalCommand], DatabaseRestoreReport]
    survey_target: Callable[[], TargetState]
    integrity: Integrity
    config_existed: bool = False
    begun: bool = False
    rechecked: bool = False
    snapshot: SnapshotResult | None = None
    database: DatabaseRestoreReport | None = None
    search_replace: SearchReplaceReport | None = None
    skipped: list[SkippedLink] = field(default_factory=list)
    wp_config_kept: bool = False

    @property
    def site_root(self) -> Path:
        return Path(self.inputs.site.site_root)

    @property
    def config_file(self) -> Path:
        located = self.inputs.relocation.config_file
        return Path(located) if located is not None else Path(self.inputs.config_path)

    @property
    def payload(self) -> Path | None:
        payload = self.inputs.payload
        return None if payload is None else Path(payload)

    @property
    def overwrites_config(self) -> bool:
        """``--overwrite-wp-config`` as planned: ``REWRITE_CONFIG``, ``OVERWRITE``."""
        return (
            self.inputs.config_policy is ConfigPolicy.OVERWRITE
            and RestorePhase.REWRITE_CONFIG in self.plan.phases
        )

    @property
    def lays_down_by_role(self) -> bool:
        """A lay-down from the Archive's ``config``-role member (else the tree's)."""
        return self.inputs.relocation.lay_down_config and any(
            m.role is MemberRole.CONFIG for m in self.inputs.view.members
        )


def execute_restore(
    plan: RestorePlan,
    inputs: RestoreInputs,
    ports: RestorePorts,
    take_snapshot: Callable[[BackupScope], SnapshotResult],
    check_database: Callable[[RestoreInputs], None],
    load_database: Callable[[RestoreInputs, RunJournalCommand], DatabaseRestoreReport],
    journal: RunJournalCommand,
    cipher: PayloadCipher | None,
    *,
    survey_target: Callable[[], TargetState],
    opted_out_by: str | None,
) -> ExecutionReport:
    """Run ``plan``'s phases, in order, and report what they did.

    Any phase's exception propagates unchanged (no catch-and-continue); the
    journal is then finished only when the failure was orderly (ADR-0021).
    A journal header naming another Mode than the plan, or a planned
    ``SNAPSHOT`` whose header names no snapshot path, is a programming error
    (``ValueError``), raised before ``PREFLIGHT``: each is carried once.

    ``survey_target`` is the root's survey (query ports only), called once,
    immediately before the first destroying phase (RT-003). ``opted_out_by``
    names what chose :attr:`SnapshotPolicy.SKIP` (``--no-safety-snapshot``,
    or the API argument) for the ``safety_snapshot_skipped`` WARNING.
    """
    if inputs.header.restore_mode is not plan.mode:
        msg = (
            f"the journal header's mode {inputs.header.restore_mode.value!r} is not "
            f"the plan's {plan.mode.value!r}"
        )
        raise ValueError(msg)
    if RestorePhase.SNAPSHOT in plan.phases and inputs.header.snapshot_path is None:
        msg = "a planned SNAPSHOT needs the journal header's snapshot_path"
        raise ValueError(msg)
    _log_snapshot_decision(plan, opted_out_by)
    run = _Run(
        plan=plan,
        inputs=inputs,
        ports=ports,
        journal=_RecordingJournal(journal),
        cipher=cipher,
        take_snapshot=take_snapshot,
        check_database=check_database,
        load_database=load_database,
        survey_target=survey_target,
        integrity=inputs.view.integrity,
    )
    try:
        for step in plan.phases:
            if step not in _CHECK_PHASES and not run.begun:
                run.journal.begin_run(inputs.header)
                run.begun = True
            if step in _DESTROYING and not run.rechecked:
                _recheck(run)
            with phase(step.value) as record:
                _dispatch(run, step, record)
    except BaseException:
        if run.begun and is_orderly_failure(tuple(run.journal.entries)):
            run.journal.finish_run()
        raise
    if run.begun:
        run.journal.finish_run()
    return ExecutionReport(
        phases=plan.phases,
        snapshot=run.snapshot,
        database=run.database,
        search_replace=run.search_replace,
        integrity=run.integrity,
        skipped_links=tuple(run.skipped),
        wp_config_kept=run.wp_config_kept,
    )


def _log_snapshot_decision(plan: RestorePlan, opted_out_by: str | None) -> None:
    """``safety_snapshot_skipped`` or ``safety_snapshot_not_needed`` (FR-8)."""
    if plan.snapshot is SnapshotPolicy.SKIP:
        _log.warning(
            "safety_snapshot_skipped",
            opted_out_by=opted_out_by or "unspecified",
            restore_mode=plan.mode.value,
        )
    elif RestorePhase.SNAPSHOT not in plan.phases:
        empty = [
            part
            for part, touched in (
                ("files", restores_files(plan.mode)),
                ("database", restores_database(plan.mode)),
            )
            if touched
        ]
        _log.info(
            "safety_snapshot_not_needed",
            empty=empty,
            reason="every part the restore mode may destroy is empty",
            restore_mode=plan.mode.value,
        )


def _recheck(run: _Run) -> None:
    """RT-003: survey the target again and refuse what outgrew the plan."""
    run.rechecked = True
    fresh = run.survey_target()
    recheck_target(run.plan, fresh)
    _log.info(
        "target_resurveyed",
        site_root_empty=fresh.site_root_empty,
        restore_mode=run.plan.mode.value,
    )


def _dispatch(run: _Run, step: RestorePhase, record: PhaseRecord) -> None:
    match step:
        case RestorePhase.PREFLIGHT:
            _preflight(run)
        case RestorePhase.VERIFY:
            _verify(run, record)
        case RestorePhase.SNAPSHOT:
            _snapshot(run, record)
        case RestorePhase.CLEAN:
            _clean(run, record)
        case RestorePhase.EXTRACT:
            _extract(run, record)
        case RestorePhase.REWRITE_CONFIG:
            _rewrite_config(run)
        case RestorePhase.LOAD_DUMP:
            _load_dump(run, record)
        case RestorePhase.SEARCH_REPLACE:
            _search_replace(run)
        case _:
            assert_never(step)


# --------------------------------------------------------------------------
# PREFLIGHT — queries only
# --------------------------------------------------------------------------


def _preflight(run: _Run) -> None:
    run.ports.preflight(run.plan.required_tools)
    if run.cipher is not None:
        run.cipher.check()
    require_patched_tarfile()
    run.config_existed = run.ports.adapters.site_files_query.file_exists(
        Path(run.inputs.config_path)
    )
    _check_config(run)
    _check_space(run)
    if RestorePhase.LOAD_DUMP in run.plan.phases:
        run.check_database(run.inputs)


def _check_config(run: _Run) -> None:
    """Refuse, before any effect, a config step the target or Archive cannot take."""
    inputs, relocation = run.inputs, run.inputs.relocation
    if relocation.lay_down_config and not run.lays_down_by_role:
        # The Archive's config is in its tree: it can be laid down only into
        # the Site Root, and only by a Mode that extracts the tree.
        in_root = _same_dir(run.config_file.parent, run.site_root)
        member = _config_member(inputs.view)
        if member is None or not in_root or not restores_files(run.plan.mode):
            raise ArchiveHasNoConfigError(str(inputs.archive.path))
    writes = RestorePhase.REWRITE_CONFIG in run.plan.phases
    if writes and not run.config_existed and not relocation.lay_down_config:
        raise TargetConfigMissingError(str(run.config_file))


def _config_member(view: ArchiveView) -> ViewMember | None:
    """The Archive's config: the ``config``-role file, else the one it names."""
    for member in view.members:
        if member.role is MemberRole.CONFIG and member.kind is MemberKind.FILE:
            return member
    if view.config_path is None:
        return None
    names = {view.config_path}
    with contextlib.suppress(UnsafeMemberPathError):
        names.add(member_path(MemberRole.SITE_ROOT, view.config_path))
    return next(
        (m for m in view.members if m.path in names and m.kind is MemberKind.FILE),
        None,
    )


def _check_space(run: _Run) -> None:
    """BL-E002-116: refuse before ``CLEAN`` what the destinations cannot hold."""
    view, phases = run.inputs.view, run.plan.phases
    extracts = RestorePhase.EXTRACT in phases
    config = _config_member(view) if run.overwrites_config else None
    sizes: dict[str, tuple[MemberRole, int]] = {}
    if all(m.size is not None for m in view.members) and view.payload is not None:
        sizes.update((m.path, (m.role, m.size or 0)) for m in view.members)
        compressed = view.payload.size
        payload = view.payload.size if view.encrypted else 0
    else:
        # An unindexed (v0) view: the tar headers' sizes, read by the probe.
        digests = run.ports.adapters.archive_probe.member_digests(run.inputs.archive)
        for name, digest in digests.items():
            located = member_relative(view, name)
            if located is not None:
                sizes[name] = (located[0], digest.size)
        archive = Path(run.inputs.archive.path)
        try:
            compressed = archive.stat().st_size
        except OSError as exc:
            raise ArchiveNotFoundError(archive) from exc
        payload = 0
    need = space_need(
        sizes.values(),
        files=extracts and restores_files(run.plan.mode),
        config=extracts and run.inputs.relocation.lay_down_config,
        dump=RestorePhase.LOAD_DUMP in phases,
        config_copy=sizes[config.path][1]
        if config is not None and config.path in sizes
        else 0,
        payload=payload,
    )
    workspace = Path(run.inputs.workspace)
    target_free = run.ports.free_bytes(run.site_root)
    workspace_free = run.ports.free_bytes(workspace)
    check_expansion(
        target_need=need.target,
        workspace_need=need.workspace,
        expanded=need.expanded,
        compressed=compressed,
        target_free=target_free,
        workspace_free=workspace_free,
    )
    claims = [
        SpaceClaim(TARGET_FILESYSTEM, _device(run.site_root), need.target, target_free),
        SpaceClaim(
            WORKSPACE_FILESYSTEM, _device(workspace), need.workspace, workspace_free
        ),
    ]
    snapshot = _snapshot_claim(run)
    if snapshot is not None:
        claims.append(snapshot)
    check_claims(claims)


def _existing(path: Path) -> Path:
    """``path``, or its nearest ancestor that exists (the root creates it later)."""
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate
    return path


def _device(path: Path) -> int:
    return _existing(path).stat().st_dev


def _snapshot_claim(run: _Run) -> SpaceClaim | None:
    """ADV-6: what the Safety Snapshot will write to its directory.

    The Site Root's current file bytes when the scope holds ``files``: an
    uncompressed upper bound for the Archive. The live database's dump size
    is not knowable before the dump, so a database-only scope claims ``0``.
    """
    scope, path = run.plan.snapshot_scope, run.inputs.header.snapshot_path
    if RestorePhase.SNAPSHOT not in run.plan.phases or scope is None or path is None:
        return None
    files = scope in {BackupScope.FILES, BackupScope.ALL}
    query = run.ports.adapters.site_files_query
    need = query.tree_bytes(run.site_root) if files else 0
    directory = _existing(Path(path).parent)
    return SpaceClaim(
        SNAPSHOT_FILESYSTEM, _device(directory), need, run.ports.free_bytes(directory)
    )


# --------------------------------------------------------------------------
# VERIFY — before the first destructive step
# --------------------------------------------------------------------------


def _verify(run: _Run, record: PhaseRecord) -> None:
    inputs = run.inputs
    view, result = verify_archive_view(
        Path(inputs.archive.path), phase_name=RestorePhase.VERIFY.value
    )
    record.fields.update(
        integrity=result.integrity.value,
        format_version=format_label(result.format_version),
    )
    if not result.passed:
        raise VerificationFailedError(result)
    if view != inputs.view:
        raise ArchiveChangedError(str(inputs.archive.path))
    run.integrity = result.integrity
    if inputs.view.encrypted:
        if run.cipher is None or run.payload is None:
            msg = "an encrypted Archive needs a cipher and a payload path"
            raise ValueError(msg)
        decrypt_payload(
            inputs.archive, inputs.view, cipher=run.cipher, destination=run.payload
        )


# --------------------------------------------------------------------------
# The destructive phases
# --------------------------------------------------------------------------


def _snapshot(run: _Run, record: PhaseRecord) -> None:
    scope = run.plan.snapshot_scope
    if scope is None:  # pragma: no cover - the plan omits SNAPSHOT then
        msg = "a planned SNAPSHOT needs a snapshot scope"
        raise ValueError(msg)
    planned = run.inputs.header.snapshot_path
    run.journal.phase_started(RestorePhase.SNAPSHOT, scope.value)
    try:
        result = run.take_snapshot(scope)
    except SnapshotFailedError:
        raise
    except Exception as exc:
        raise SnapshotFailedError(planned, exc) from exc
    _require_snapshot(run, result, scope)
    run.snapshot = result
    record.bytes = result.bytes
    record.fields.update(path=str(result.path), performed=result.performed)
    if result.performed:
        _log.info(
            "safety_snapshot_written",
            path=str(result.path),
            scope=result.scope.value,
            bytes=result.bytes,
            encrypted=result.encrypted,
            integrity=None if result.integrity is None else result.integrity.value,
            restore_mode=run.plan.mode.value,
        )
    else:
        _log.info(
            "safety_snapshot_planned",
            path=str(result.path),
            scope=result.scope.value,
            integrity="would verify",
            restore_mode=run.plan.mode.value,
        )
    run.journal.phase_completed(RestorePhase.SNAPSHOT)


def _destroys(adapters: AdapterSet) -> bool:
    """Whether any command half a destroying phase reaches performs effects."""
    return any(
        type(getattr(adapters, name)).__module__ != noop.__name__
        for name in _DESTROYING_HALVES
    )


def _require_snapshot(run: _Run, result: SnapshotResult, scope: BackupScope) -> None:
    """ADV-5: refuse a result that does not prove the planned, verified snapshot."""
    planned = run.inputs.header.snapshot_path
    if str(result.path) != planned:
        reason = (
            f"the snapshot was written to {str(result.path)!r}, not the planned path"
        )
        raise SnapshotFailedError(planned, reason)
    if result.scope is not scope:
        reason = f"the snapshot covers {result.scope.value!r}, not {scope.value!r}"
        raise SnapshotFailedError(planned, reason)
    if not result.performed:
        if _destroys(run.ports.adapters):
            reason = "the snapshot was not performed, and the restore would destroy"
            raise SnapshotFailedError(planned, reason)
        return
    if result.integrity is not Integrity.VERIFIED or result.bytes <= 0:
        integrity = None if result.integrity is None else result.integrity.value
        reason = f"integrity {integrity}, {result.bytes} bytes"
        raise SnapshotFailedError(planned, reason)


def snapshot_result(
    path: PurePosixPath,
    *,
    scope: BackupScope,
    encrypted: bool,
    bytes: int,  # noqa: A002 - the BackupResult field it carries
    performed: bool,
) -> SnapshotResult:
    """The :class:`SnapshotResult` of a joined backup, verified after publish.

    E003-S02-006's ``take_snapshot`` passes the returned ``BackupResult``'s
    fields. When ``performed``, :func:`~l3io.wp.backup.adapters.tar.verify_archive`
    reads the published file again and anything but a passing ``VERIFIED``
    result raises :class:`SnapshotFailedError` naming it; the file is left in
    place, since it may hold the only copy. When not ``performed`` (a Dry
    Run's no-op store) nothing is read and the result reports "would verify"
    (``integrity=None``).
    """
    if not performed:
        return SnapshotResult(
            path=path,
            scope=scope,
            encrypted=encrypted,
            bytes=0,
            integrity=None,
            performed=False,
        )
    try:
        verified = verify_archive(Path(path))
    except BackupError as exc:
        raise SnapshotFailedError(str(path), exc) from exc
    if not verified.passed or verified.integrity is not Integrity.VERIFIED:
        reason = f"post-publish verification gave {verified.integrity.value}"
        if not verified.passed:
            first = verified.failures[0].reason if verified.failures else "unknown"
            reason = f"post-publish verification failed: {first}"
        raise SnapshotFailedError(str(path), reason)
    return SnapshotResult(
        path=path,
        scope=scope,
        encrypted=encrypted,
        bytes=bytes,
        integrity=Integrity.VERIFIED,
        performed=True,
    )


def _clean(run: _Run, record: PhaseRecord) -> None:
    in_root = _same_dir(Path(run.inputs.config_path).parent, run.site_root)
    keep = _KEPT_CONFIG if in_root else frozenset()
    run.journal.phase_started(RestorePhase.CLEAN, str(run.site_root))
    removed = run.ports.adapters.site_files_command.clean(run.site_root, keep=keep)
    record.fields["removed"] = removed
    run.journal.phase_completed(RestorePhase.CLEAN)


def _extract(run: _Run, record: PhaseRecord) -> None:
    inputs, adapters = run.inputs, run.ports.adapters
    lay_down = inputs.relocation.lay_down_config
    by_role = run.lays_down_by_role
    config_file = run.config_file
    if lay_down:
        run.journal.phase_started(RestorePhase.EXTRACT, f"config:{config_file}")
        run.journal.step_completed(RestorePhase.EXTRACT, STEP_CONFIG_STARTED)
    if by_role:
        laid = adapters.site_files_command.extract(
            inputs.archive,
            config_file.parent,
            inputs.view,
            roles=frozenset({MemberRole.CONFIG}),
            payload=run.payload,
        )
        record.bytes += laid.bytes
        run.journal.step_completed(RestorePhase.EXTRACT, STEP_CONFIG)
    if restores_files(run.plan.mode):
        # The tree's copy is excluded when the target keeps its own, or when
        # the config-role lay-down placed one in the Site Root; a lay-down from
        # the tree is this very extraction (``_check_config``).
        laid_in_root = by_role and _same_dir(config_file.parent, run.site_root)
        keeps = run.config_existed or laid_in_root
        run.journal.phase_started(RestorePhase.EXTRACT, str(run.site_root))
        result = adapters.site_files_command.extract(
            inputs.archive,
            run.site_root,
            inputs.view,
            roles=frozenset({MemberRole.SITE_ROOT}),
            exclude=_KEPT_CONFIG if keeps else frozenset(),
            payload=run.payload,
        )
        record.bytes += result.bytes
        run.skipped.extend(result.skipped_links)
        if run.config_existed and inputs.config_policy is ConfigPolicy.KEEP:
            run.wp_config_kept = True
            _log.info("wp_config_kept", path=str(inputs.config_path))
    if lay_down and not by_role:
        run.journal.step_completed(RestorePhase.EXTRACT, STEP_CONFIG)
    run.journal.phase_completed(RestorePhase.EXTRACT)


def _same_dir(left: Path, right: Path) -> bool:
    """Whether two paths name one directory, symlinks and ``..`` resolved."""
    return os.path.realpath(left) == os.path.realpath(right)


def _archive_config(run: _Run) -> Path:
    """The Archive's config, in the Run workspace at mode ``0600``.

    A ``config``-role member is laid down by ``site_files_command`` so a Dry
    Run's no-op half writes nothing. A config that lives in the tree is read
    and written here (a port to extract one member is E003-S02-006's).
    """
    inputs = run.inputs
    member = _config_member(inputs.view)
    if member is None:
        raise ArchiveHasNoConfigError(str(inputs.archive.path))
    if member.role is MemberRole.CONFIG:
        directory = Path(inputs.workspace) / _ARCHIVE_CONFIG_DIR
        run.ports.adapters.site_files_command.extract(
            inputs.archive,
            directory,
            inputs.view,
            roles=frozenset({MemberRole.CONFIG}),
            payload=run.payload,
        )
        return directory / CONFIG_FILE_NAME
    body = run.ports.adapters.archive_probe.read_member(
        inputs.archive,
        inputs.view,
        member.path,
        max_bytes=_CONFIG_MAX_BYTES,
        payload=run.payload,
    )
    path = Path(inputs.workspace) / _ARCHIVE_CONFIG
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, _CONFIG_MODE)
        with os.fdopen(fd, "wb") as out:
            out.write(body)
    except OSError as exc:
        raise ArchiveWriteError(path, exc.strerror or type(exc).__name__) from exc
    return path


def _rewrite_config(run: _Run) -> None:
    adapters, inputs = run.ports.adapters, run.inputs
    config_file = run.config_file
    run.journal.phase_started(RestorePhase.REWRITE_CONFIG, str(config_file))
    if run.overwrites_config:
        saved = inputs.header.saved_config
        if saved is None:
            msg = "--overwrite-wp-config needs the journal header's saved_config"
            raise ValueError(msg)
        adapters.config_command.save_copy(config_file, Path(saved))
        run.journal.step_completed(RestorePhase.REWRITE_CONFIG, STEP_CONFIG_SAVED)
        prior = adapters.config_query.read_constants(config_file, CUTOVER_KEYS)
        source = _archive_config(run)
        adapters.config_command.replace_file(config_file, source, keep=dict(prior))
        run.journal.step_completed(RestorePhase.REWRITE_CONFIG, STEP_CONFIG_REPLACED)
        _log.warning("wp_config_overwritten", path=str(config_file), saved=saved)
    rewrite = inputs.relocation.rewrite_values
    if rewrite:
        adapters.config_command.set_constants(config_file, dict(rewrite))
    run.journal.phase_completed(RestorePhase.REWRITE_CONFIG)


def _load_dump(run: _Run, record: PhaseRecord) -> None:
    inputs = run.inputs
    target = inputs.relocation.load_database or inputs.site.db_name
    run.journal.phase_started(RestorePhase.LOAD_DUMP, target)
    run.database = run.load_database(inputs, run.journal)
    record.bytes = run.database.loaded_bytes
    run.journal.phase_completed(RestorePhase.LOAD_DUMP)


def _search_replace(run: _Run) -> None:
    urls = run.inputs.relocation.search_replace
    if urls is None:  # pragma: no cover - the plan omits SEARCH_REPLACE then
        msg = "a planned SEARCH_REPLACE needs the relocation's URLs"
        raise ValueError(msg)
    run.journal.phase_started(RestorePhase.SEARCH_REPLACE, str(run.site_root))
    command = run.ports.adapters.search_replace
    run.search_replace = command.relocate_content(run.site_root, urls)
    run.journal.phase_completed(RestorePhase.SEARCH_REPLACE)
