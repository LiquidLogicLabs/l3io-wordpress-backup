"""AD-1, AD-5, AD-12, AD-17 — the restore plan, and the pure values its Run carries.

:func:`build_restore_plan` turns a resolved Restore Mode and what is known
about the target into a :class:`RestorePlan`: the phases to run, as a
**filter** over :data:`~l3io.wp.backup.domain.restore.RESTORE_PHASE_ORDER`
(never a list written in an order, so a plan is a subsequence of the enum by
construction and :class:`RestorePlan` re-asserts it), the tools preflight
checks, and the ordered actions a Dry Run prints. The per-mode predicate is
composed from ``domain/restore.py``'s four ``match``-based functions; this
module adds no second mode table.

Also declared here, once, for the stories that produce or consume them:
:func:`snapshot_scope`, :func:`snapshot_location`, :class:`SnapshotResult`,
:class:`DatabaseRestoreReport`, the journal step names (ADR-0020),
:func:`is_orderly_failure` (ADR-0021), :class:`RestoreRelocation` (Epic 7's
seam), :class:`RestoreInputs`, :class:`ExecutionReport`, and the BL-E002-116
size and free-space rule :func:`check_expansion`.

Pure: no filesystem, clock or server is read here (``tools/check_purity.py``).
Paths are ``PurePath`` values; the caller resolves them.
"""

from __future__ import annotations

import posixpath
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePath, PurePosixPath
from typing import Final, Literal, assert_never

from l3io.wp.backup.core.backup import DUMP_REQUIREMENT
from l3io.wp.backup.domain.archive_format import (
    ArchiveView,
    Integrity,
    MemberKind,
    MemberRole,
    SkippedLink,
    member_relative,
)
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.database import (
    DatabaseLocation,
    DatabaseTargetState,
    EnsureOutcome,
)
from l3io.wp.backup.domain.errors import ArchiveHasNoDumpError, RefusedError
from l3io.wp.backup.domain.journal import JournalEntry, JournalHeader
from l3io.wp.backup.domain.preflight import ToolRequirement, merge_requirements
from l3io.wp.backup.domain.restore import (
    RESTORE_PHASE_ORDER,
    ConfigPolicy,
    OverrideSet,
    RestoreMode,
    RestorePhase,
    SnapshotPolicy,
    clears_site_root,
    is_subsequence,
    removes_database,
    restores_database,
    restores_files,
)
from l3io.wp.backup.domain.search_replace import SearchReplaceReport, UrlRelocation
from l3io.wp.backup.domain.site import SiteDescriptor
from l3io.wp.backup.domain.storage_target import StorageTarget

__all__ = [
    "CONFIG_FILE_NAME",
    "CUTOVER_KEYS",
    "FREE_SPACE_MARGIN",
    "MAX_EXPANSION_RATIO",
    "NO_RELOCATION",
    "STEP_CHECK_FAILED",
    "STEP_CONFIG",
    "STEP_CONFIG_REPLACED",
    "STEP_CONFIG_SAVED",
    "STEP_CONFIG_STARTED",
    "STEP_CUTOVER",
    "STEP_CUTOVER_READBACK_FAILED",
    "STEP_CUTOVER_STARTED",
    "STEP_LOSER_DROPPED",
    "STEP_LOSER_DROP_STARTED",
    "STEP_STAGING_DROPPED",
    "TARGET_FILESYSTEM",
    "WORKSPACE_FILESYSTEM",
    "ArchiveHasNoConfigError",
    "DatabaseRestoreReport",
    "ExecutionReport",
    "ExpansionRatioError",
    "InsufficientSpaceError",
    "PlannedRestoreAction",
    "RestoreInputs",
    "RestorePlan",
    "RestoreRelocation",
    "SnapshotResult",
    "SpaceNeed",
    "TargetState",
    "UnsafeSnapshotLocationError",
    "build_restore_plan",
    "check_expansion",
    "is_orderly_failure",
    "planned_skipped_links",
    "snapshot_location",
    "snapshot_scope",
    "space_need",
]

#: The name WordPress gives its configuration file.
CONFIG_FILE_NAME: Final = "wp-config.php"

#: The ``wp-config.php`` keys only ``LOAD_DUMP``'s cutover writes (AD-17 as
#: amended 2026-10-27); :class:`RestoreRelocation` refuses them in
#: ``rewrite_values``.
CUTOVER_KEYS: Final = ("DB_HOST", "DB_USER", "DB_PASSWORD", "DB_NAME")

#: BL-E002-116: the share of a filesystem's free space a restore leaves unused.
FREE_SPACE_MARGIN: Final = 0.05
#: BL-E002-116: the largest expanded-to-compressed ratio a restore accepts.
MAX_EXPANSION_RATIO: Final = 200
#: How :class:`InsufficientSpaceError` names the filesystem that is short.
TARGET_FILESYSTEM: Final = "restore target (Site Root filesystem)"
WORKSPACE_FILESYSTEM: Final = "Run workspace filesystem"

# The named journal steps of ADR-0020. Every story that journals or reads a
# step uses these constants.
STEP_CONFIG_STARTED: Final = "config_started"
STEP_CONFIG: Final = "config"
STEP_CONFIG_SAVED: Final = "config_saved"
STEP_CONFIG_REPLACED: Final = "config_replaced"
#: Prefix: followed by the key name, e.g. ``"cutover_started:DB_NAME"``.
STEP_CUTOVER_STARTED: Final = "cutover_started:"
#: Prefix: followed by the key name, e.g. ``"cutover:DB_NAME"``.
STEP_CUTOVER: Final = "cutover:"
STEP_CHECK_FAILED: Final = "check_failed"
STEP_CUTOVER_READBACK_FAILED: Final = "cutover_readback_failed"
STEP_STAGING_DROPPED: Final = "staging_dropped"
STEP_LOSER_DROP_STARTED: Final = "loser_drop_started"
STEP_LOSER_DROPPED: Final = "loser_dropped"


# --------------------------------------------------------------------------
# Errors (Epic 3's, defined in the module that raises them)
# --------------------------------------------------------------------------


class UnsafeSnapshotLocationError(RefusedError):
    """The Safety Snapshot directory is inside the Site Root or its config dir."""

    def __init__(self, requested: PurePosixPath, inside: PurePosixPath) -> None:
        self.requested = requested
        self.inside = inside
        super().__init__(
            f"the snapshot directory {str(requested)!r} is inside "
            f"{str(inside)!r}, which may be web-served; choose a directory "
            "outside the Site Root and its wp-config.php directory"
        )


class ArchiveHasNoConfigError(RefusedError):
    """``--overwrite-wp-config`` and the Archive carries no ``wp-config.php``."""

    def __init__(self, archive: str) -> None:
        self.archive = archive
        super().__init__(
            f"archive {archive!r} holds no wp-config.php to overwrite the target's with"
        )


class InsufficientSpaceError(RefusedError):
    """A destination filesystem cannot hold what the restore writes there."""

    def __init__(self, filesystem: str, *, needed: int, free: int) -> None:
        self.filesystem = filesystem
        self.needed = needed
        self.free = free
        usable = int(free * (1 - FREE_SPACE_MARGIN))
        super().__init__(
            f"the {filesystem} needs {needed} bytes and has {free} bytes free "
            f"({usable} usable after the {FREE_SPACE_MARGIN:.0%} margin)"
        )


class ExpansionRatioError(RefusedError):
    """The Archive expands by more than :data:`MAX_EXPANSION_RATIO` times."""

    def __init__(self, *, expanded: int, compressed: int) -> None:
        self.expanded = expanded
        self.compressed = compressed
        super().__init__(
            f"the archive expands from {compressed} to {expanded} bytes, over "
            f"the {MAX_EXPANSION_RATIO}x expansion limit"
        )


# --------------------------------------------------------------------------
# The target, the relocation seam and the plan
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TargetState:
    """What the restore root learned about the target, from query ports only."""

    site_root_empty: bool
    database: DatabaseTargetState | None
    config_exists: bool


@dataclass(frozen=True, slots=True)
class RestoreRelocation:
    """Epic 7's seam (decision 2 of 2026-10-27). Epic 3 computes no field.

    ``rewrite_values`` may hold no :data:`CUTOVER_KEYS` key: ``__post_init__``
    raises ``ValueError``, so writing ``DB_*`` outside ``LOAD_DUMP``'s cutover
    is unrepresentable. ``cutover_password`` says whether ``DB_PASSWORD`` is
    in the cutover set; its value is never in this type.
    """

    rewrite_values: tuple[tuple[str, str], ...]
    load_database: str | None
    load_location: DatabaseLocation | None
    cutover_host: str | None
    cutover_user: str | None
    cutover_password: bool
    search_replace: UrlRelocation | None
    config_file: PurePosixPath | None
    lay_down_config: bool
    required_tools: tuple[ToolRequirement, ...]
    actions: tuple[tuple[RestorePhase, str], ...]
    undo: tuple[tuple[RestorePhase, str], ...]

    def __post_init__(self) -> None:
        refused = sorted(k for k, _ in self.rewrite_values if k in CUTOVER_KEYS)
        if refused:
            msg = (
                f"rewrite_values may not hold a cutover key {list(CUTOVER_KEYS)}; "
                f"refused: {refused}"
            )
            raise ValueError(msg)

    @classmethod
    def none(cls) -> RestoreRelocation:
        """No relocation: every field empty, ``None`` or ``False``."""
        return cls(
            rewrite_values=(),
            load_database=None,
            load_location=None,
            cutover_host=None,
            cutover_user=None,
            cutover_password=False,
            search_replace=None,
            config_file=None,
            lay_down_config=False,
            required_tools=(),
            actions=(),
            undo=(),
        )


#: No relocation.
NO_RELOCATION: Final = RestoreRelocation.none()


@dataclass(frozen=True, slots=True)
class PlannedRestoreAction:
    """One action a restore plans, as reported (``cli/backup.py``'s twin)."""

    ordinal: int
    phase: RestorePhase
    text: str
    fields: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class RestorePlan:
    """What a restore will do, in AD-17's order, and what it needs to do it."""

    mode: RestoreMode
    phases: tuple[RestorePhase, ...]
    snapshot: SnapshotPolicy
    snapshot_scope: BackupScope | None
    required_tools: tuple[ToolRequirement, ...]
    actions: tuple[PlannedRestoreAction, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "phases", tuple(self.phases))
        if not is_subsequence(self.phases):
            names = [p.value for p in self.phases]
            msg = f"restore phases {names} are not a subsequence of AD-17's order"
            raise ValueError(msg)


# --------------------------------------------------------------------------
# The Run's values (the executor's inputs and outputs)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SnapshotResult:
    """What the Safety Snapshot wrote; E003-S02-003's ``snapshot_result`` builds it."""

    path: PurePosixPath
    scope: BackupScope
    encrypted: bool
    bytes: int
    integrity: Integrity
    performed: bool


@dataclass(frozen=True, slots=True)
class DatabaseRestoreReport:
    """What ``LOAD_DUMP`` did. ``restored`` only when load, check and cutover held."""

    live_database_before: str
    live_database_after: str
    ensure: EnsureOutcome
    loaded_bytes: int
    load_duration_ms: float
    previous_database: Literal["dropped", "retained", "absent"]
    restored: bool
    performed: bool


@dataclass(frozen=True, slots=True)
class RestoreInputs:
    """The Run's facts the executor's arms use, built once by the restore root."""

    site: SiteDescriptor
    view: ArchiveView
    archive: StorageTarget
    run_id: str
    relocation: RestoreRelocation
    header: JournalHeader
    config_policy: ConfigPolicy
    config_path: PurePath
    payload: PurePath | None
    workspace: PurePath


@dataclass(frozen=True, slots=True)
class ExecutionReport:
    """What a completed restore did, phase by phase."""

    phases: tuple[RestorePhase, ...]
    snapshot: SnapshotResult | None
    database: DatabaseRestoreReport | None
    search_replace: SearchReplaceReport | None
    integrity: Integrity
    skipped_links: tuple[SkippedLink, ...]
    wp_config_kept: bool


# --------------------------------------------------------------------------
# Snapshot scope and location (AD-12)
# --------------------------------------------------------------------------


def _database_empty(target: TargetState) -> bool:
    db = target.database
    return db is None or not db.database_exists or db.table_count == 0


def snapshot_scope(mode: RestoreMode, target: TargetState) -> BackupScope | None:
    """What the Safety Snapshot must capture before ``mode`` runs on ``target``.

    Every part the Mode may destroy, less the parts that are empty in
    ``target``; ``None`` only when every part it touches is empty.
    """
    match mode:
        case RestoreMode.FILES | RestoreMode.FILES_CLEAN:
            wanted = BackupScope.FILES
        case RestoreMode.DATABASE | RestoreMode.DATABASE_CLEAN:
            wanted = BackupScope.DATABASE
        case RestoreMode.ALL | RestoreMode.ALL_CLEAN:
            wanted = BackupScope.ALL
        case _:
            assert_never(mode)
    files = (
        wanted in {BackupScope.FILES, BackupScope.ALL} and not target.site_root_empty
    )
    database = wanted in {BackupScope.DATABASE, BackupScope.ALL} and not (
        _database_empty(target)
    )
    if files and database:
        return BackupScope.ALL
    if files:
        return BackupScope.FILES
    if database:
        return BackupScope.DATABASE
    return None


def snapshot_location(
    site_root: PurePosixPath, config_dir: PurePosixPath, requested: PurePosixPath
) -> PurePosixPath:
    """``requested``, refused when it may be web-served (AD-12).

    Refused (:class:`UnsafeSnapshotLocationError`) when equal to or inside
    ``site_root``, or inside ``config_dir`` when that differs from
    ``site_root`` (FR-29's one-level-up ``wp-config.php``). ``requested`` is
    canonicalised by the caller.
    """
    for root in (site_root, config_dir):
        if requested == root or requested.is_relative_to(root):
            raise UnsafeSnapshotLocationError(requested, root)
    return requested


# --------------------------------------------------------------------------
# The orderly-failure predicate (ADR-0021)
# --------------------------------------------------------------------------

_FILE_PHASES: Final = frozenset(
    {RestorePhase.CLEAN, RestorePhase.EXTRACT, RestorePhase.REWRITE_CONFIG}
)


def is_orderly_failure(entries: tuple[JournalEntry, ...]) -> bool:
    """Whether a failed Run left the Site as it found it (ADR-0021).

    True only when no ``CLEAN``, ``EXTRACT`` or ``REWRITE_CONFIG`` phase
    started, no ``"cutover:*"`` step completed, ``"staging_dropped"``
    completed, and ``"check_failed"`` or ``"cutover_readback_failed"`` did.
    """
    steps = {e.step for e in entries if e.event == "step_completed" and e.step}
    if any(e.event == "phase_started" and e.phase in _FILE_PHASES for e in entries):
        return False
    if any(step.startswith(STEP_CUTOVER) for step in steps):
        return False
    return STEP_STAGING_DROPPED in steps and bool(
        steps & {STEP_CHECK_FAILED, STEP_CUTOVER_READBACK_FAILED}
    )


# --------------------------------------------------------------------------
# BL-E002-116 — size, free space and expansion
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SpaceNeed:
    """What each destination must hold, and the Archive's expansion."""

    target: int
    workspace: int
    expanded: int


def space_need(
    sizes: Iterable[tuple[MemberRole, int]],
    *,
    files: bool,
    config: bool,
    dump: bool,
    config_copy: int,
    payload: int,
) -> SpaceNeed:
    """Sum member ``sizes`` per destination (E003 pre-build re-check 2).

    The target holds the ``site-root`` members when ``files`` and the
    ``config`` members when ``config`` (a lay-down); never the Dump. The
    workspace holds the decrypted ``payload`` (``0`` for a plain Archive),
    the Dump when ``dump``, and the ``config_copy`` bytes.
    """
    target = workspace = expanded = 0
    for role, size in sizes:
        expanded += size
        match role:
            case MemberRole.SITE_ROOT:
                target += size if files else 0
            case MemberRole.CONFIG:
                target += size if config else 0
            case MemberRole.DUMP:
                workspace += size if dump else 0
            case _:
                assert_never(role)
    return SpaceNeed(
        target=target, workspace=workspace + payload + config_copy, expanded=expanded
    )


def check_expansion(
    *,
    target_need: int,
    workspace_need: int,
    expanded: int,
    compressed: int,
    target_free: int,
    workspace_free: int,
) -> None:
    """Refuse a restore its destinations cannot hold, or a decompression bomb.

    :class:`ExpansionRatioError` when ``expanded / compressed`` exceeds
    :data:`MAX_EXPANSION_RATIO`; :class:`InsufficientSpaceError` when a need
    exceeds its filesystem's free space less :data:`FREE_SPACE_MARGIN`.
    """
    if expanded > MAX_EXPANSION_RATIO * compressed:
        raise ExpansionRatioError(expanded=expanded, compressed=compressed)
    usable = 1 - FREE_SPACE_MARGIN
    if target_need > target_free * usable:
        raise InsufficientSpaceError(
            TARGET_FILESYSTEM, needed=target_need, free=target_free
        )
    if workspace_need > workspace_free * usable:
        raise InsufficientSpaceError(
            WORKSPACE_FILESYSTEM, needed=workspace_need, free=workspace_free
        )


# --------------------------------------------------------------------------
# The links a real Run would skip (computed from the view, a query)
# --------------------------------------------------------------------------


def planned_skipped_links(view: ArchiveView) -> tuple[SkippedLink, ...]:
    """The ``site-root`` symlinks extraction would skip: absolute or escaping.

    From ``ViewMember.link_target``; a v0 view carries none, so its links are
    reported at extraction instead. The rule is lexical (``posixpath.normpath``
    of the link's directory joined with its target); extraction decides with
    ``tarfile.data_filter``, which resolves with ``realpath``. The two agree
    for every link the Archive itself defines; they can differ only where a
    directory on the target is already a symlink, which extraction alone sees.
    """
    skipped: list[SkippedLink] = []
    for member in view.members:
        if member.kind is not MemberKind.SYMLINK or member.link_target is None:
            continue
        located = member_relative(view, member.path)
        if located is None or located[0] is not MemberRole.SITE_ROOT:
            continue
        relative, target = located[1], member.link_target
        if target.startswith("/"):
            skipped.append(SkippedLink(relative, target, "absolute"))
            continue
        resolved = posixpath.normpath(
            posixpath.join(posixpath.dirname(relative), target)
        )
        if resolved == ".." or resolved.startswith("../"):
            skipped.append(SkippedLink(relative, target, "outside_destination"))
    return tuple(skipped)


# --------------------------------------------------------------------------
# The plan
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Facts:
    """What the per-phase predicate and the action lines read."""

    site: SiteDescriptor
    mode: RestoreMode
    view: ArchiveView
    target: TargetState
    archive: StorageTarget
    relocation: RestoreRelocation
    snapshot: SnapshotPolicy
    scope: BackupScope | None
    location: PurePosixPath | None
    config_file: PurePosixPath
    config_policy: ConfigPolicy


def _kept(facts: _Facts, phase: RestorePhase) -> bool:
    """The per-mode predicate: whether ``phase`` is in the plan."""
    mode, relocation = facts.mode, facts.relocation
    match phase:
        case RestorePhase.PREFLIGHT | RestorePhase.VERIFY:
            return True
        case RestorePhase.SNAPSHOT:
            return facts.snapshot is SnapshotPolicy.TAKE and facts.scope is not None
        case RestorePhase.CLEAN:
            return clears_site_root(mode)
        case RestorePhase.EXTRACT:
            return restores_files(mode) or relocation.lay_down_config
        case RestorePhase.REWRITE_CONFIG:
            overwrite = facts.config_policy is ConfigPolicy.OVERWRITE
            return bool(relocation.rewrite_values) or (
                overwrite and facts.target.config_exists
            )
        case RestorePhase.LOAD_DUMP:
            return restores_database(mode)
        case RestorePhase.SEARCH_REPLACE:
            return relocation.search_replace is not None and restores_database(mode)
        case _:
            assert_never(phase)


def _line(facts: _Facts, phase: RestorePhase) -> tuple[str, dict[str, object]]:
    """The action line for ``phase``: host, port and name, never a credential."""
    site, view = facts.site, facts.view
    root = str(site.site_root)
    match phase:
        case RestorePhase.PREFLIGHT:
            checks = "required tools, the interpreter's tarfile fixes, free space"
            if restores_database(facts.mode):
                checks += " and the database checks"
            return f"preflight: {checks}", {}
        case RestorePhase.VERIFY:
            decrypt = " and decrypt its payload" if view.encrypted else ""
            return f"verify {facts.archive.path}{decrypt}", {
                "archive": str(facts.archive.path),
                "encrypted": view.encrypted,
            }
        case RestorePhase.SNAPSHOT:
            scope = "" if facts.scope is None else facts.scope.value
            return f"safety snapshot of {scope} to {facts.location}", {
                "scope": scope,
                "directory": str(facts.location),
            }
        case RestorePhase.CLEAN:
            return f"clean {root}", {"site_root": root}
        case RestorePhase.EXTRACT:
            parts = []
            if facts.relocation.lay_down_config:
                parts.append(f"lay down {facts.config_file}")
            if restores_files(facts.mode):
                parts.append(f"extract the Archive's files into {root}")
            return "; then ".join(parts), {
                "site_root": root,
                "lay_down_config": facts.relocation.lay_down_config,
            }
        case RestorePhase.REWRITE_CONFIG:
            parts = []
            if facts.config_policy is ConfigPolicy.OVERWRITE:
                parts.append(
                    f"overwrite {facts.config_file} by the Archive's copy, "
                    "keeping its DB_* values (a copy saved first)"
                )
            keys = [k for k, _ in facts.relocation.rewrite_values]
            if keys:
                parts.append(f"set {', '.join(keys)} in {facts.config_file}")
            return "; then ".join(parts), {
                "config_file": str(facts.config_file),
                "keys": keys,
            }
        case RestorePhase.LOAD_DUMP:
            location = facts.relocation.load_location
            host = site.db_host if location is None else location.host
            port = site.db_port if location is None else location.port
            name = (
                facts.relocation.load_database
                or f"a staging database for {site.db_name}"
            )
            drop = removes_database(facts.mode)
            tail = f" and drop the previous {site.db_name}" if drop else ""
            return f"load the Dump into {name} on {host}:{port}, then cut over{tail}", {
                "host": host,
                "port": port,
                "database": facts.relocation.load_database or site.db_name,
                "live_database": site.db_name,
                "drop_previous": drop,
            }
        case RestorePhase.SEARCH_REPLACE:
            return "replace the stored site URLs across the content", {}
        case _:
            assert_never(phase)


def _require_relocation_text(
    relocation: RestoreRelocation, phases: tuple[RestorePhase, ...]
) -> None:
    for kind, pairs in (("action", relocation.actions), ("undo", relocation.undo)):
        for named, _ in pairs:
            if named not in phases:
                msg = f"relocation {kind} names {named.value!r}, which the plan omits"
                raise ValueError(msg)
    undone = {p for p, _ in relocation.undo}
    planned = []
    if RestorePhase.REWRITE_CONFIG in phases and relocation.rewrite_values:
        planned.append(RestorePhase.REWRITE_CONFIG)
    if RestorePhase.SEARCH_REPLACE in phases:
        planned.append(RestorePhase.SEARCH_REPLACE)
    for phase in planned:
        if phase not in undone:
            msg = f"relocation phase {phase.value!r} is planned with no undo text"
            raise ValueError(msg)


def build_restore_plan(
    site: SiteDescriptor,
    mode: RestoreMode,
    overrides: OverrideSet,
    view: ArchiveView,
    target: TargetState,
    *,
    archive: StorageTarget,
    relocation: RestoreRelocation,
    snapshot: SnapshotPolicy,
    snapshot_dir: PurePath,
    config_dir: PurePath,
    config_policy: ConfigPolicy,
    extra_tools: tuple[ToolRequirement, ...] = (),
) -> RestorePlan:
    """The plan for restoring ``view`` onto ``site`` in exactly ``mode``.

    Refuses, before any effect: :class:`ArchiveHasNoDumpError` when ``mode``
    names the database and the Archive has no Dump;
    :class:`ArchiveHasNoConfigError` under ``OVERWRITE`` with no config in the
    Archive; :class:`UnsafeSnapshotLocationError` when ``snapshot`` is
    ``TAKE`` and ``snapshot_dir`` may be web-served. A relocation action or
    undo naming an omitted phase, or a planned relocation phase without undo
    text, raises ``ValueError``. ``overrides`` (the FR-7 waivers) is carried
    for E003-S02-001's refusals and changes no phase.
    """
    del overrides  # FR-7's refusals are E003-S02-001's; no phase depends on them.
    if restores_database(mode) and not view.dump_present:
        raise ArchiveHasNoDumpError(
            archive=str(archive.path), backup_scope=view.backup_scope
        )
    if config_policy is ConfigPolicy.OVERWRITE and view.config_path is None:
        raise ArchiveHasNoConfigError(str(archive.path))
    scope = snapshot_scope(mode, target)
    location = None
    if snapshot is SnapshotPolicy.TAKE:
        location = snapshot_location(
            site.site_root, PurePosixPath(config_dir), PurePosixPath(snapshot_dir)
        )
    config_file = relocation.config_file or PurePosixPath(config_dir) / CONFIG_FILE_NAME
    facts = _Facts(
        site=site,
        mode=mode,
        view=view,
        target=target,
        archive=archive,
        relocation=relocation,
        snapshot=snapshot,
        scope=scope,
        location=location,
        config_file=config_file,
        config_policy=config_policy,
    )
    phases = tuple(p for p in RESTORE_PHASE_ORDER if _kept(facts, p))
    _require_relocation_text(relocation, phases)
    actions: list[PlannedRestoreAction] = []
    for phase in phases:
        text, fields = _line(facts, phase)
        lines: list[tuple[str, Mapping[str, object]]] = [(text, fields)]
        lines += [(t, {}) for p, t in relocation.actions if p is phase]
        for line_text, line_fields in lines:
            actions.append(
                PlannedRestoreAction(len(actions) + 1, phase, line_text, line_fields)
            )
    dumps = scope in {BackupScope.DATABASE, BackupScope.ALL}
    own = (DUMP_REQUIREMENT,) if RestorePhase.SNAPSHOT in phases and dumps else ()
    return RestorePlan(
        mode=mode,
        phases=phases,
        snapshot=snapshot,
        snapshot_scope=scope,
        required_tools=merge_requirements(own, relocation.required_tools, extra_tools),
        actions=tuple(actions),
    )
