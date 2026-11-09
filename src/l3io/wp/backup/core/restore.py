"""AD-1, AD-5, AD-12, AD-17, AD-25 — the restore plan, and the values its Run carries.

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
:class:`SnapshotFailedError`, :func:`recheck_target` (RT-003),
:class:`DatabaseRestoreReport` with its pure derivation
:func:`database_restored` (FR-9), the journal step names (ADR-0020),
:func:`is_orderly_failure` (ADR-0021), :class:`RestoreRelocation` (Epic 7's
seam), :class:`RestoreInputs`, :class:`ExecutionReport`, and the BL-E002-116
size and free-space rule :func:`check_expansion`, with
:func:`check_claims` summing the destinations that share a filesystem
(ADV-6).

FR-7's guard is :func:`check_target`, which :func:`build_restore_plan` calls
before any phase is planned: a part the Mode restores whose target is not
empty refuses (:class:`NonEmptyTargetError`) unless the Run's
:class:`~l3io.wp.backup.domain.restore.OverrideSet` waives that part. Owner
decision 57 narrows the files part: a non-empty Site Root that looks like a
WordPress site (any of :data:`WORDPRESS_MARKERS`) needs no ``--force``; the
Safety Snapshot covers the overwrite. The database part's narrowing (tables,
but none with the site's prefix) arrives with the direct load (part 2):
:func:`_database_needs_waiver` is its seam. The
authorizing flags come from AD-25's table
(:func:`~l3io.wp.backup.domain.flag_map.authorizing_combination`) and are
rendered here in the domain spelling (:func:`render_flags`); the CLI's own
spelling is ``cli/restore_request.py``'s.

Pure: no filesystem, clock or server is read here (``tools/check_purity.py``).
Paths are ``PurePath`` values; the caller resolves them.
"""

from __future__ import annotations

import posixpath
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePath, PurePosixPath
from typing import Final, Literal, assert_never

from l3io.wp.backup.core.backup import DUMP_REQUIREMENT
from l3io.wp.backup.domain.archive_format import (
    WP_CONFIG_NAME,
    ArchiveView,
    ConfigLocation,
    Integrity,
    MemberKind,
    MemberRole,
    SkippedLink,
    member_relative,
    records_link_targets,
)
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.database import (
    DatabaseLocation,
    DatabaseTargetState,
    EnsureOutcome,
    LoadResult,
)
from l3io.wp.backup.domain.errors import ArchiveHasNoDumpError, RefusedError
from l3io.wp.backup.domain.flag_map import FlagCombination, authorizing_combination
from l3io.wp.backup.domain.journal import JournalHeader
from l3io.wp.backup.domain.preflight import ToolRequirement, merge_requirements
from l3io.wp.backup.domain.restore import (
    RESTORE_PHASE_ORDER,
    ConfigPolicy,
    OverrideSet,
    RestoreMode,
    RestorePart,
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
    "MAX_DATABASE_NAME",
    "NO_RELOCATION",
    "STAGING_INFIX",
    "WORDPRESS_MARKERS",
    "ArchiveHasNoConfigError",
    "DatabaseRestoreReport",
    "ExecutionReport",
    "NoConfigForCutoverError",
    "NonEmptyTargetError",
    "PlannedRestoreAction",
    "RestoreInputs",
    "RestorePlan",
    "RestoreRelocation",
    "SnapshotFailedError",
    "SnapshotResult",
    "TargetChangedError",
    "TargetState",
    "UnsafeSnapshotLocationError",
    "build_restore_plan",
    "check_target",
    "database_report_fields",
    "database_restored",
    "load_names",
    "planned_cutover_keys",
    "planned_cutover_targets",
    "planned_skipped_links",
    "recheck_target",
    "render_flags",
    "snapshot_location",
    "snapshot_scope",
    "staging_name",
]

#: The name WordPress gives its configuration file (``WP_CONFIG_NAME``).
CONFIG_FILE_NAME: Final = WP_CONFIG_NAME
#: Owner decision 57: a Site Root holding any of these looks like WordPress,
#: so a restore over it needs no ``--force``.
WORDPRESS_MARKERS: Final = (WP_CONFIG_NAME, "wp-includes", "wp-content")

#: The ``wp-config.php`` keys only ``LOAD_DUMP``'s cutover writes (AD-17 as
#: amended 2026-10-27); :class:`RestoreRelocation` refuses them in
#: ``rewrite_values``.
CUTOVER_KEYS: Final = ("DB_HOST", "DB_USER", "DB_PASSWORD", "DB_NAME")


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


def _cause_text(cause: BaseException) -> str:
    """The cause's type and, for an ``OSError``, its errno text; never its message."""
    strerror = getattr(cause, "strerror", None)
    name = type(cause).__name__
    return f"{name}: {strerror}" if isinstance(strerror, str) else name


class SnapshotFailedError(RefusedError):
    """FR-8: the Safety Snapshot was not written and verified; nothing destroyed.

    ``cause`` is what failed (a write, the publish, the post-publish verify,
    a result that does not prove a snapshot); ``path`` is the snapshot's
    planned path, where a written-but-unverified file is left in place, since
    it may hold the only copy.
    """

    def __init__(self, path: str | None, cause: BaseException | str) -> None:
        self.path = path
        self.cause = cause
        reason = cause if isinstance(cause, str) else _cause_text(cause)
        where = "" if path is None else f" {path!r}"
        super().__init__(
            f"the safety snapshot{where} was not written and verified ({reason}); "
            "the restore stopped before anything was destroyed"
        )


class TargetChangedError(RefusedError):
    """RT-003: the target gained content the plan's snapshot does not cover."""

    def __init__(self, planned: BackupScope | None, found: BackupScope) -> None:
        self.planned = planned
        self.found = found
        covered = "nothing" if planned is None else repr(planned.value)
        super().__init__(
            f"the target changed after planning: the safety snapshot covers "
            f"{covered}, and {found.value!r} now holds content; "
            "the restore stopped before anything was destroyed"
        )


class ArchiveHasNoConfigError(RefusedError):
    """``--overwrite-wp-config`` and the Archive carries no ``wp-config.php``."""

    def __init__(self, archive: str) -> None:
        self.archive = archive
        super().__init__(
            f"archive {archive!r} holds no wp-config.php to overwrite the target's "
            "with; omit --overwrite-wp-config to keep the target's"
        )


class NoConfigForCutoverError(RefusedError):
    """A database Mode, and no ``wp-config.php`` will exist for the cutover.

    Refused at plan time, before ``SNAPSHOT``: the cutover's ``set()`` never
    creates a file, so it would otherwise be found only after the load.
    ``path`` is where the cutover would write; ``searched`` is every place a
    target ``wp-config.php`` was looked for (``path`` alone when not given).
    """

    def __init__(self, path: str, *, searched: tuple[str, ...] = ()) -> None:
        self.path = path
        self.searched = searched or (path,)
        where = ", ".join(repr(place) for place in self.searched)
        super().__init__(
            f"the restore loads the database but no wp-config.php will exist at "
            f"{path!r} for the cutover (searched {where}: the target has none and "
            "nothing lays one down); nothing was changed"
        )


def render_flags(flags: FlagCombination) -> str:
    """``flags`` in the domain spelling: ``mode=database force``.

    Valued flags first (``dest=value``), then each set boolean flag by its
    ``dest``; a false boolean is omitted. ``cli/restore_request.py`` renders the CLI
    spelling; ``core/`` cannot import ``cli/``.
    """
    valued = [f"{dest}={value}" for dest, value in flags if not isinstance(value, bool)]
    switches = [dest for dest, value in flags if value is True]
    return " ".join(valued + switches)


class NonEmptyTargetError(RefusedError):
    """FR-7: a part the Mode restores is not empty, and its refusal is not waived.

    ``parts`` names every unauthorized part in one refusal. ``findings``
    pairs each non-empty target with what is in it — ``("'/srv/www'",
    "3 entries")``, ``("db:3306/wp", "12 tables")`` — in ``parts`` order, for
    a structured log; ``found`` and ``detail`` are the same, ``"; "``-joined.
    ``authorizing`` is the table's combination that selects ``mode`` and
    waives every one of ``parts``. Names paths and ``host:port/name`` only,
    never a credential.
    """

    def __init__(
        self,
        mode: RestoreMode,
        *,
        parts: tuple[RestorePart, ...],
        findings: tuple[tuple[str, str], ...],
        authorizing: FlagCombination,
    ) -> None:
        self.mode = mode
        self.parts = parts
        self.findings = findings
        self.found = "; ".join(where for where, _ in findings)
        self.detail = "; ".join(what for _, what in findings)
        self.authorizing = authorizing
        named = "; ".join(f"{where} ({what})" for where, what in findings)
        super().__init__(
            f"refusing to restore {mode.value!r} over a non-empty target "
            f"({' and '.join(parts)}): found {named}; nothing was changed. "
            f"{render_flags(authorizing)} would authorize it"
        )


# --------------------------------------------------------------------------
# The target, the relocation seam and the plan
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TargetState:
    """What the restore root learned about the target, from query ports only.

    ``database`` is the survey of the **live** database; ``load_target`` the
    same survey of the operator-named load database (``--new-db-name``), else
    ``None``. ``site_root``, ``site_root_entries`` and the two locations are
    the facts an FR-7 refusal names, so :func:`check_target` reads nothing
    else. ``site_root_wordpress`` says the Site Root holds one of
    :data:`WORDPRESS_MARKERS` (owner decision 57); ``False``, the default,
    keeps FR-7's refusal for a non-empty Site Root.
    """

    site_root_empty: bool
    database: DatabaseTargetState | None
    config_exists: bool
    site_root: PurePosixPath
    site_root_entries: int
    database_location: DatabaseLocation | None
    load_target: DatabaseTargetState | None
    load_location: DatabaseLocation | None
    site_root_wordpress: bool = False


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
    #: The FR-7 waivers the plan was checked under; :func:`recheck_target`
    #: re-applies them to the executor's fresh survey (RT-003).
    overrides: OverrideSet
    required_tools: tuple[ToolRequirement, ...]
    actions: tuple[PlannedRestoreAction, ...]
    #: The ``site-root`` links extraction will skip (:func:`planned_skipped_links`),
    #: which a Dry Run lists after its actions (owner decision 41): empty when
    #: the plan extracts no Site files, ``None`` when the Archive records no
    #: link targets (v0), so they are reported at extraction instead.
    skipped_links: tuple[SkippedLink, ...] | None = ()

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
    """What the Safety Snapshot wrote; ``restore_runner.snapshot_result`` builds it.

    ``integrity`` is what the post-publish verification proved, and ``None``
    when ``performed`` is false: a Dry Run verifies nothing ("would verify").
    """

    path: PurePosixPath
    scope: BackupScope
    encrypted: bool
    bytes: int
    integrity: Integrity | None
    performed: bool


@dataclass(frozen=True, slots=True)
class DatabaseRestoreReport:
    """What ``LOAD_DUMP`` did. ``restored`` only when load, check and cutover held.

    ``restored`` is :func:`database_restored` of the effects' results; an
    existing database is never a restored one. ``performed`` is ``False`` for
    a Dry Run, whose report says "would load" and never ``restored``.

    ``previous_database`` is what became of the database the cutover left:
    ``"dropped"`` (or, in a Dry Run, would be), ``"retained"``, ``"none"``
    when the administrator's survey found no live database, and
    ``"absent"`` when the Dump loaded in place (no cutover target to leave).
    """

    live_database_before: str
    live_database_after: str
    ensure: EnsureOutcome
    loaded_bytes: int
    load_duration_ms: float
    previous_database: Literal["dropped", "retained", "none", "absent"]
    restored: bool
    performed: bool


def database_restored(
    loaded: LoadResult, *, check_passed: bool, cutover_written: bool
) -> bool:
    """FR-9: the database was restored only if all three facts hold.

    A performed load, the post-load check passed, and every cutover key
    written. Taken from the effects' **results** (ADR-0022), never by
    re-reading the server, so a load that did not happen — a Dry Run's
    not-performed ``LoadResult`` included — can never be reported restored.
    """
    return loaded.performed and check_passed and cutover_written


def database_report_fields(report: DatabaseRestoreReport) -> dict[str, object]:
    """Every :class:`DatabaseRestoreReport` field, flat: the log record and JSON."""
    ensure = report.ensure
    return {
        "live_database_before": report.live_database_before,
        "live_database_after": report.live_database_after,
        "created_database": ensure.created_database,
        "created_user": ensure.created_user,
        "password_set": ensure.password_set,
        "granted": ensure.granted,
        "warnings": list(ensure.warnings),
        "loaded_bytes": report.loaded_bytes,
        "load_duration_ms": report.load_duration_ms,
        "previous_database": report.previous_database,
        "restored": report.restored,
        "performed": report.performed,
    }


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
    #: The administrator's survey of the live database (AD-29): whether it
    #: existed. ``False`` makes the report's ``previous_database`` ``"none"``.
    live_database_exists: bool
    #: The Archive as the operator named it, for error text: ``archive`` is
    #: the workspace copy, deleted with the Run (BL-E003-068). ``None`` names
    #: ``archive`` itself.
    archive_name: str | None = None

    @property
    def archive_label(self) -> str:
        """The Archive's name for an error the operator reads."""
        return (
            str(self.archive.path) if self.archive_name is None else self.archive_name
        )


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


def _survey_empty(db: DatabaseTargetState | None) -> bool:
    """Absent, or present with no table, view, routine or event: empty.

    For FR-7 and the snapshot. A database holding only stored routines or
    events is not empty (BL-E003-053).
    """
    return (
        db is None
        or not db.database_exists
        or db.table_count + db.routine_count + db.event_count == 0
    )


def _database_empty(target: TargetState) -> bool:
    return _survey_empty(target.database)


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

    **What "web-served" covers.** Only those two directories: this rule
    cannot know a web server's document roots, so a ``requested`` directory
    served from elsewhere is not detected. The root's default, the per-user
    state directory, is never a document root by convention.
    """
    for root in (site_root, config_dir):
        if requested == root or requested.is_relative_to(root):
            raise UnsafeSnapshotLocationError(requested, root)
    return requested


# --------------------------------------------------------------------------
# FR-7 — the non-empty-target guard (AD-25's waivers)
# --------------------------------------------------------------------------


def _where(location: DatabaseLocation | None, fallback: str) -> str:
    if location is None:
        return fallback
    return f"{location.host}:{location.port}/{location.name}"


def _count(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _contents(db: DatabaseTargetState) -> str:
    """What makes ``db`` non-empty: its tables, then any routines and events."""
    parts = [_count(db.table_count, "table", "tables")]
    if db.routine_count:
        parts.append(_count(db.routine_count, "routine", "routines"))
    if db.event_count:
        parts.append(_count(db.event_count, "event", "events"))
    return ", ".join(parts)


def _database_needs_waiver(db: DatabaseTargetState | None) -> bool:
    """Whether restoring over ``db`` needs ``--force``: today, any content.

    SEAM (owner decision 57, database side; part 2, with the direct load):
    a database with tables but none carrying the site's table prefix needs
    ``--force``, and a recognisable WordPress database
    (``DatabaseTargetState.is_wordpress``, surveyed with the site's prefix)
    does not. Until then every non-empty database does, as before.
    """
    return db is not None and not _survey_empty(db)


def check_target(
    mode: RestoreMode, overrides: OverrideSet, target: TargetState
) -> None:
    """Refuse ``mode`` over a non-empty part of ``target`` that is not waived.

    Each part ``mode`` restores is checked against its own ``overrides``
    member, so waiving ``files`` never waives ``database``. The ``files``
    part needs the waiver only when the Site Root is not empty and does not
    look like WordPress (owner decision 57). The ``database`` part is
    non-empty when the live database or the operator-named load database
    exists with a table (:func:`_database_needs_waiver`). Every unauthorized
    part is named in one :class:`NonEmptyTargetError`; nothing is read
    beyond ``target``.
    """
    parts: list[RestorePart] = []
    findings: list[tuple[str, str]] = []
    if (
        restores_files(mode)
        and not target.site_root_empty
        and not target.site_root_wordpress
        and not overrides.waives("files")
    ):
        parts.append("files")
        entries = _count(target.site_root_entries, "entry", "entries")
        findings.append(
            (
                repr(str(target.site_root)),
                f"{entries}, none of {', '.join(WORDPRESS_MARKERS)}",
            )
        )
    occupied = [
        (_where(location, name), _contents(db))
        for db, location, name in (
            (target.database, target.database_location, "the live database"),
            (target.load_target, target.load_location, "the load database"),
        )
        if _database_needs_waiver(db) and db is not None
    ]
    if restores_database(mode) and occupied and not overrides.waives("database"):
        parts.append("database")
        findings += occupied
    if not parts:
        return
    raise NonEmptyTargetError(
        mode,
        parts=tuple(parts),
        findings=tuple(findings),
        authorizing=authorizing_combination(mode, parts[0]),
    )


def _covers(planned: BackupScope | None, found: BackupScope) -> bool:
    if planned is None:
        return False
    return planned is BackupScope.ALL or planned is found


def recheck_target(plan: RestorePlan, target: TargetState) -> None:
    """RT-003/ADV-4: refuse when ``target``, surveyed again, outgrew ``plan``.

    The executor calls this immediately before the first destroying phase,
    with a fresh :class:`TargetState`. :func:`check_target` runs again under
    the plan's waivers (:class:`NonEmptyTargetError`), and when the plan takes
    the Safety Snapshot, every part that now holds content must be one the
    snapshot covered (:class:`TargetChangedError`). Under
    :attr:`SnapshotPolicy.SKIP` the operator opted out, so only FR-7 applies.
    """
    check_target(plan.mode, plan.overrides, target)
    if plan.snapshot is not SnapshotPolicy.TAKE:
        return
    found = snapshot_scope(plan.mode, target)
    if found is None:
        return
    parts = (
        (BackupScope.FILES, BackupScope.DATABASE)
        if found is BackupScope.ALL
        else (found,)
    )
    for part in parts:
        if not _covers(plan.snapshot_scope, part):
            raise TargetChangedError(plan.snapshot_scope, part)


# --------------------------------------------------------------------------
# The staging database and the cutover set (AD-18.3, E003-S02-005)
# --------------------------------------------------------------------------

#: database's ``MAX_DATABASE_NAME``: :func:`staging_name` stays within it.
MAX_DATABASE_NAME: Final = 64
#: Between the live name and the Run ID in a staging database's name.
STAGING_INFIX: Final = "_l3s_"
_STAGING_LIVE_CHARS: Final = MAX_DATABASE_NAME - len(STAGING_INFIX) - 8
_STAGING_RUN_CHARS: Final = 8


def staging_name(live: str, run_id: str) -> str:
    """The staging database a Run loads into: ``<live[:51]>_l3s_<run id[:8]>``.

    Applies the length rule only (at most :data:`MAX_DATABASE_NAME`
    characters); the name is validated by ``adapters/staging.py`` with
    database's ``validate_database_name`` before its first use (AD-1).
    """
    run = run_id.replace("-", "")[:_STAGING_RUN_CHARS]
    return f"{live[:_STAGING_LIVE_CHARS]}{STAGING_INFIX}{run}"


def load_names(
    live: str, run_id: str, relocation: RestoreRelocation
) -> tuple[str, str | None]:
    """``(load database, staging database)``; the staging one ``None`` when named.

    ``--new-db-name`` (``relocation.load_database``) loads straight into the
    operator's database; otherwise the Run's :func:`staging_name`.
    """
    if relocation.load_database:
        return relocation.load_database, None
    staging = staging_name(live, run_id)
    return staging, staging


def planned_cutover_keys(relocation: RestoreRelocation) -> tuple[str, ...]:
    """The :data:`CUTOVER_KEYS` this Run's cutover writes, in that order.

    ``DB_NAME`` always; ``DB_HOST``/``DB_USER`` when the relocation names
    them; ``DB_PASSWORD`` when ``relocation.cutover_password``.
    """
    wanted = {"DB_NAME"}
    if relocation.cutover_host is not None:
        wanted.add("DB_HOST")
    if relocation.cutover_user is not None:
        wanted.add("DB_USER")
    if relocation.cutover_password:
        wanted.add("DB_PASSWORD")
    return tuple(k for k in CUTOVER_KEYS if k in wanted)


def planned_cutover_targets(
    load_database: str, relocation: RestoreRelocation
) -> dict[str, str]:
    """The cutover's non-secret targets (``DB_NAME``/``DB_HOST``/``DB_USER``).

    ``DB_PASSWORD`` is never here: it is resolved at its write (AD-18.1).
    """
    targets = {"DB_NAME": load_database}
    if relocation.cutover_host is not None:
        targets["DB_HOST"] = relocation.cutover_host
    if relocation.cutover_user is not None:
        targets["DB_USER"] = relocation.cutover_user
    return targets


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
    #: The binaries the plan's requirements name (``extra_tools``, which
    #: carries relocation's ``WP_CLI_REQUIREMENT``), for the PREFLIGHT line.
    tools: tuple[ToolRequirement, ...] = ()


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
            # BL-E007-006: each binary named with its floor, from the plan's
            # own requirements (WP-CLI's is core/relocation.WP_CLI_REQUIREMENT).
            named = [
                tool.name
                if tool.minimum_version is None
                else f"{tool.name} >= {'.'.join(map(str, tool.minimum_version))}"
                for tool in facts.tools
                if tool.name != DUMP_REQUIREMENT.name
            ]
            if named:
                checks += f" ({', '.join(named)})"
            return f"preflight: {checks}", {"tools": named} if named else {}
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


def _config_at_cutover(
    site: SiteDescriptor,
    mode: RestoreMode,
    view: ArchiveView,
    target: TargetState,
    relocation: RestoreRelocation,
    config_file: PurePosixPath,
) -> bool:
    """Whether a ``wp-config.php`` will exist at ``config_file`` for the cutover.

    The target has one, a lay-down places one, or ``EXTRACT`` brings the
    Archive's in-tree copy into a Site Root that holds the config path. The
    copy is in the tree when ``config_path`` is ``ConfigLocation.SITE_ROOT``,
    for a v0 Archive as for a v1 one (owner decision 46).
    """
    if target.config_exists or relocation.lay_down_config:
        return True
    in_tree = view.config_path == ConfigLocation.SITE_ROOT
    return restores_files(mode) and in_tree and config_file.parent == site.site_root


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
    Archive; :class:`NoConfigForCutoverError` when ``mode`` loads the
    database and no ``wp-config.php`` will exist for the cutover;
    :class:`UnsafeSnapshotLocationError` when ``snapshot`` is
    ``TAKE``, or ``config_policy`` is ``OVERWRITE`` (the replaced
    ``wp-config.php`` is saved there), and ``snapshot_dir`` may be
    web-served. First of all, :func:`check_target` refuses
    (:class:`NonEmptyTargetError`) a non-empty part of ``target`` that
    ``overrides`` (the FR-7 waivers) does not waive;
    ``overrides`` changes no phase. A relocation action or undo naming an
    omitted phase, or a planned relocation phase without undo text, raises
    ``ValueError``.
    """
    check_target(mode, overrides, target)
    if restores_database(mode) and not view.dump_present:
        raise ArchiveHasNoDumpError(
            archive=str(archive.path), backup_scope=view.backup_scope
        )
    if config_policy is ConfigPolicy.OVERWRITE and view.config_path is None:
        raise ArchiveHasNoConfigError(str(archive.path))
    config_file = relocation.config_file or PurePosixPath(config_dir) / CONFIG_FILE_NAME
    if restores_database(mode) and not _config_at_cutover(
        site, mode, view, target, relocation, config_file
    ):
        # Without --wp-config the target's config is looked for in the Site
        # Root and one level up (FR-29); name both (F-10).
        searched = (
            (config_file,)
            if relocation.config_file is not None
            else (
                site.site_root / CONFIG_FILE_NAME,
                site.site_root.parent / CONFIG_FILE_NAME,
            )
        )
        raise NoConfigForCutoverError(
            str(config_file),
            searched=tuple(dict.fromkeys(str(place) for place in searched)),
        )
    scope = snapshot_scope(mode, target)
    location = None
    if snapshot is SnapshotPolicy.TAKE or config_policy is ConfigPolicy.OVERWRITE:
        # The ``OVERWRITE`` copy of the replaced ``wp-config.php`` is saved in
        # ``snapshot_dir`` too, so the web-served guard holds without a
        # Safety Snapshot (RT-S02-006).
        checked = snapshot_location(
            site.site_root, PurePosixPath(config_dir), PurePosixPath(snapshot_dir)
        )
        location = checked if snapshot is SnapshotPolicy.TAKE else None
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
        tools=merge_requirements(relocation.required_tools, extra_tools),
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
    skipped: tuple[SkippedLink, ...] | None = ()
    if RestorePhase.EXTRACT in phases and restores_files(mode):
        skipped = planned_skipped_links(view) if records_link_targets(view) else None
    return RestorePlan(
        mode=mode,
        phases=phases,
        snapshot=snapshot,
        snapshot_scope=scope,
        overrides=overrides,
        required_tools=merge_requirements(own, relocation.required_tools, extra_tools),
        actions=tuple(actions),
        skipped_links=skipped,
    )
