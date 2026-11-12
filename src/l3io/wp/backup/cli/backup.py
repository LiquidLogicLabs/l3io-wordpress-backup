"""AD-1/AD-16/AD-20 — the backup composition root, its driver and the plan executor.

Three layers, each calling only the next:

- :func:`run_backup` (the CLI root) takes the Run's ``SecretRegistry`` from
  ``cli.main``, translates the flags into a :class:`BackupRequest` (the
  passphrase and cipher resolved into that registry first), makes **one**
  call to :func:`drive_backup`, renders what it returns on stdout and returns
  ``ExitCode.OK``; a failure is raised for the root to map and report,
  redacted, on stderr (FR-21, ADR-0018). A refused ``--whatIf`` Run still
  prints, on stdout, the actions planned before it was refused.
- :func:`drive_backup` (the argument-free driver the CLI and Epic 6's ``api/``
  share) canonicalises the Site Root, opens or joins the Run
  (``cli.context.join_or_open``) holding that Site's Site Lock (AD-19,
  ``cli.adapter_sets.real_site_lock`` unless a ``site_lock`` is given) — so a
  CLI and a library caller are locked identically, before the first read of
  Site state, and a Dry Run takes it for real — reads the Site Descriptor, builds the
  WordPress credential source through ``cli/credentials.py``, plans, logs each
  planned action as ``planned_action`` and hands the actions to its caller's
  ``on_plan``, binds the adapter set only when it opened the Run, and calls
  :func:`backup`. It never prints. A Dry Run request joining a real Run is
  refused (``UsageError``, ``cli.context.join_or_open``).
- :func:`backup` (the Run-Context-accepting entry point the Safety Snapshot
  calls, E003-S02-003) checks the final name (a query), runs preflight
  (AD-13) and then :func:`execute_backup_plan` inside ``ctx`` with its
  workspace, adapters and clock, and never opens a Run.

**Dry Run (AD-4).** Selection happens here and nowhere else: when the driver
opened the Run it binds ``dry_run_adapters(real_adapters(...))`` for a
``BackupRequest(dry_run=True)`` and ``real_adapters(...)`` otherwise. Nothing
below branches on it: the same plan runs against real queries and no-op
commands, so credential resolution, preflight and every query run for real
and nothing is dumped, collected, written or published (ADR-0019).

:func:`execute_backup_plan` performs each step of a
:class:`~l3io.wp.backup.core.backup.BackupPlan` in order through the bound
command ports only — ``DUMP`` through ``DatabaseCommand.dump_to``; collection,
``ENCRYPT`` and ``WRITE_ARCHIVE`` through ``ArchiveWriterCommand.write``;
``VERIFY`` and ``PUBLISH`` inside ``StagedArchive.publish()``. It calls no
writer, dump or store function directly. The Archive is written into a staged
file beside its final name and becomes visible only through the store's
``publish()``, after the verifier passes (AD-20); any failure before that
discards the staged file.

**Bindings (E008-S01-001).** :func:`run_backup` resolves the passphrase only
with ``--encrypt-with-passphrase`` (``cli/passphrase.require_passphrase``),
builds the cipher with ``adapters/crypto.build_payload_cipher`` and passes its
``required_tools()`` as ``extra_tools``; it builds the exclusion set from
``--exclude``/``--no-default-excludes`` (``ExclusionSet.from_patterns``,
wrapped in ``LoggedExclusionPolicy``), and with no exclusion flag the request
takes :func:`default_exclusion_policy`. ``--list-default-excludes`` prints
``enumerate_defaults()`` and returns without a Run.

**Where the Archive goes (owner decision 60).** ``--archive`` names it;
without it, the default name is written into ``--archive-dir``, else the
current directory when that is outside the Site Root, else the per-user data
directory (:func:`default_archive_directory`), so a default target is never
inside the Site Root. The choice is logged (INFO ``archive_target_chosen``)
and the Dry Run's ``publish`` line names it. A target the operator put inside
the Site Root is written, with WARNING ``archive_target_in_site_root``: the
web server may serve it.

**The database host (owner decision 54).** Logged as INFO ``database_host``
and named in the Dry Run's ``dump`` line; no confirmation is asked. A
``DB_HOST`` that cannot be right (an unresolved PHP expression, a malformed
host or port) is refused while the Site Descriptor is read.

**Started as root (owner decision 67).** :func:`run_backup` refuses a
root-owned Site Root first (``cli/privileges.owner_to_run_as``), then reads
what only root may read: the passphrase, and, for a Run that dumps, the
database credentials, resolved through the whole chain into a
``ResolvedCredentialSpec`` (:func:`_read_as_root`). It then switches to the
Site Root's owner (``cli/privileges.switch_to``), so the Run's state
directory and Site Lock are the ones a restore of the site uses and the
Archive is the owner's, and refuses an Archive directory that owner cannot
write (``ArchiveDirectoryNotWritableError``) before the Run opens, a Dry Run
too. The library never switches: :func:`drive_backup` refuses a Run as root
(``RootBackupRefusedError``, owner decision 68).
"""

from __future__ import annotations

import argparse
import dataclasses
import fnmatch
import os
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Final, cast
from urllib.parse import unquote, urlsplit

import platformdirs
import structlog

from l3io.wp.backup.adapters.crypto import PassphraseCipher, build_payload_cipher
from l3io.wp.backup.adapters.database import (
    LocatedConfig,
    check_db_tls,
    database_host_is_local,
    locate_wp_config,
    require_wp_config_credentials,
    resolve_wordpress_now,
    site_descriptor_from_wp_config,
)
from l3io.wp.backup.adapters.exclusions import LoggedExclusionPolicy, log_path_excluded
from l3io.wp.backup.adapters.lock import APP_NAME
from l3io.wp.backup.adapters.phase import phase
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.cli import privileges
from l3io.wp.backup.cli.adapter_sets import (
    dry_run_adapters,
    real_adapters,
    real_site_lock,
)
from l3io.wp.backup.cli.context import RunContext, join_or_open
from l3io.wp.backup.cli.credentials import (
    db_tls_setting,
    explicit_secret,
    note_password_on_command_line,
    wordpress_source,
    wordpress_spec,
)
from l3io.wp.backup.cli.output import (
    RunWarning,
    WarningSink,
    emit_result,
    emit_warning,
)
from l3io.wp.backup.cli.passphrase import (
    note_unused_passphrase,
    require_passphrase,
    terminal_prompt,
)
from l3io.wp.backup.cli.preflight import require_tools, root_preflight
from l3io.wp.backup.cli.scope import backup_scope_from_cli
from l3io.wp.backup.cli.settings import (
    SETTING_NAMES,
    config_key,
    env_name,
    setting_origins,
    typed_settings,
)
from l3io.wp.backup.core.backup import BackupPlan, BackupStep, build_backup_plan
from l3io.wp.backup.core.exclusions import ExclusionSet, enumerate_defaults
from l3io.wp.backup.domain.archive_format import (
    DUMP_RELATIVE,
    FORMAT_VERSION,
    Integrity,
    Manifest,
    ManifestExclusions,
    ManifestMember,
    ManifestOrigin,
    created_at_text,
    pending_payload,
    source_from_descriptor,
)
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.credentials import CredentialSpec, ResolvedCredentialSpec
from l3io.wp.backup.domain.errors import (
    ArchiveExistsError,
    SiteRootNotFoundError,
    UsageError,
)
from l3io.wp.backup.domain.exclusion import (
    ExcludedEntry,
    ExclusionDecision,
    ExclusionSource,
)
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.domain.preflight import ToolRequirement
from l3io.wp.backup.domain.secret import Secret
from l3io.wp.backup.domain.site import SiteDescriptor
from l3io.wp.backup.domain.storage_target import (
    DEFAULT_ARCHIVE_NAME_GLOBS,
    LATEST,
    StorageTarget,
    default_archive_name,
    parse_storage_target,
)
from l3io.wp.backup.ports.archive import (
    ArchiveStoreCommand,
    ArchiveWriterCommand,
    StagedArchive,
)
from l3io.wp.backup.ports.crypto import PayloadCipher
from l3io.wp.backup.ports.database import DatabaseCommand, DbTls
from l3io.wp.backup.ports.exclusion import ExclusionPolicy
from l3io.wp.backup.ports.lock import SiteLock
from l3io.wp.backup.ports.preflight import Preflight
from l3io.wp.backup.ports.tracing import Tracer

__all__ = [
    "DATA_DIRECTORY_ARCHIVES",
    "DRY_RUN_TRAILER",
    "NO_ENVIRONMENT",
    "BackupRequest",
    "BackupResult",
    "PlannedAction",
    "backup",
    "default_archive_directory",
    "default_exclusion_policy",
    "drive_backup",
    "execute_backup_plan",
    "plan_actions",
    "plan_backup",
    "run_backup",
    "storage_target_from_cli",
]

_log = structlog.get_logger(__name__)

_EXCLUSION_SOURCES: Final = tuple(s.value for s in ExclusionSource)
#: A driver's ``environ`` when its caller gives none: nothing is read.
NO_ENVIRONMENT: Final[Mapping[str, str]] = MappingProxyType({})
_LOCAL_HOSTS: Final = ("", "localhost")


@dataclass(frozen=True, slots=True)
class BackupResult:
    """What a completed backup published.

    ``performed`` is the value ``StagedArchive.publish()`` returned — ``False``
    only from the no-op store, so a caller knows a Dry Run wrote nothing.
    ``integrity`` is what the publish gate's verification proved, and
    ``None`` when nothing was published: a Dry Run verifies nothing.
    ``plan`` is the plan that ran and ``actions`` its rendered lines, in order
    (filled by :func:`drive_backup`, which knows the credential source).
    """

    path: PurePosixPath
    scope: BackupScope
    bytes: int
    encrypted: bool
    integrity: Integrity | None
    performed: bool
    plan: BackupPlan
    actions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PlannedAction:
    """One action a backup plans, as reported: its ordinal, step and line."""

    ordinal: int
    step: str
    text: str
    fields: Mapping[str, object]


#: The last line of a successful backup Dry Run (UX-2, BL-E002-102).
DRY_RUN_TRAILER: Final = (
    "Dry Run complete: the plan above was checked, and nothing was written or "
    "published. A Dry Run does not detect write-time failures, such as a full "
    "disk or a file that cannot be read"
)

#: Names this package gives its own Archives: the default names, today's
#: ``wpbak-*`` and the legacy ``wpbackup2-*`` (owner decision 82), and the
#: store's staged ``.<name>.<random>.partial`` beside each.
_OWN_ARCHIVE_GLOBS: Final = (
    *DEFAULT_ARCHIVE_NAME_GLOBS,
    *(f".{each}.*.partial" for each in DEFAULT_ARCHIVE_NAME_GLOBS),
)


@dataclass(frozen=True, slots=True)
class _SkipStaged:
    """An exclusion policy that also leaves out this package's own Archives.

    The staged file is created before the Site Root is collected; with a
    target inside the Site Root (the default is the current directory) it
    would otherwise be collected into itself. Earlier Archives under their
    default name, and staged files a killed Run left, are left out too, so a
    backup run from the docroot does not double in size every run
    (BL-E002-100). This holds whatever the exclusion flags say.
    """

    inner: ExclusionPolicy
    site_root: Path
    staged: Path

    @property
    def source(self) -> str:
        return self.inner.source

    @property
    def patterns(self) -> tuple[str, ...]:
        return self.inner.patterns

    def decide(self, relative: PurePosixPath, *, is_dir: bool) -> ExclusionDecision:
        own = None
        if not is_dir and self.site_root / relative == self.staged:
            own = "the Archive being written"
        elif not is_dir and any(
            fnmatch.fnmatchcase(relative.name, g) for g in _OWN_ARCHIVE_GLOBS
        ):
            own = "an earlier wpbackup2 Archive"
        if own is None:
            return self.inner.decide(relative, is_dir=is_dir)
        # BL-E004-045: logged like every other exclusion, so the path_excluded
        # records match the Manifest's excluded entries.
        log_path_excluded(relative, own)
        return ExclusionDecision(excluded=True, pattern=own, protected=False)


def default_exclusion_policy() -> ExclusionPolicy:
    """The one place the default exclusion set is resolved (AD-26 reasoning).

    The CLI with no exclusion flag and a library :class:`BackupRequest` built
    without ``exclusion`` both land here: Epic 4's ``ExclusionSet.default()``,
    its decisions logged (``LoggedExclusionPolicy``). A fresh policy per call,
    since the logged wrapper announces itself once.
    """
    return LoggedExclusionPolicy(ExclusionSet.default())


@dataclass(frozen=True, slots=True)
class BackupRequest:
    """Everything a backup needs from its caller; :func:`drive_backup` does the rest.

    ``scope`` has no default (AD-5). ``credentials`` is the backup-owned spec,
    never a database type (ADR-0015); ``None`` means ``wp-config.php``.
    ``target=None`` means the default Archive name in ``archive_dir``, else
    in :func:`default_archive_directory` (owner decision 60); ``site_name``
    is that name's ``<site>`` part, the database name when ``None`` (owner
    decision 82).
    ``dry_run`` selects the no-op command adapters (AD-4) when the driver
    opens the Run; ``extra_tools`` reaches preflight through the plan.
    ``db_tls`` is ``--db-tls``/``--db-tls-ca`` (owner decision 56; ``None``
    requires no TLS). ``target_chosen_by`` names what chose ``target``, for
    the ``archive_target_chosen`` record (the flag, or the library's
    parameter). ``credentials`` is a ``ResolvedCredentialSpec`` only when the
    CLI, started as root, resolved them before it switched (owner decision
    67).
    """

    site_root: Path
    scope: BackupScope
    target: StorageTarget | None
    wp_config: Path | None
    credentials: CredentialSpec | ResolvedCredentialSpec | None
    cipher: PayloadCipher | None
    exclusion: ExclusionPolicy = field(default_factory=default_exclusion_policy)
    dry_run: bool = False
    extra_tools: tuple[ToolRequirement, ...] = ()
    archive_dir: Path | None = None
    db_tls: DbTls = field(default_factory=DbTls)
    site_name: str | None = None
    target_chosen_by: str = field(default="--archive", compare=False)


#: Under the per-user data directory: where a default Archive goes when the
#: current directory is inside the Site Root (owner decision 60).
DATA_DIRECTORY_ARCHIVES: Final = "archives"
_DATA_DIRECTORY: Final = "the per-user data directory"


def default_archive_directory(site_root: Path) -> tuple[Path, str]:
    """Where the default Archive goes, and what chose it (owner decision 60).

    The current directory when it is outside the canonical ``site_root``, else
    ``<user data dir>/l3io-wp-backup/archives``: never inside the Site Root.
    Restore's ``--archive latest`` searches the same directory.
    """
    cwd = Path.cwd().resolve()
    if not cwd.is_relative_to(site_root):
        return cwd, "the current directory"
    return platformdirs.user_data_path(APP_NAME) / DATA_DIRECTORY_ARCHIVES, (
        _DATA_DIRECTORY
    )


def _log_database_host(site: SiteDescriptor) -> None:
    """Owner decision 54: the host the credentials go to, logged, never asked."""
    _log.info(
        "database_host",
        db_host=site.db_host,
        db_port=site.db_port,
        local=database_host_is_local(site.db_host),
    )


def _warn_target_inside(
    target: StorageTarget,
    site_root: Path,
    *,
    on_warning: WarningSink | None,
) -> None:
    """Owner decision 60: a target inside the Site Root proceeds, with a WARNING."""
    resolved = Path(target.path).resolve()
    if not resolved.is_relative_to(site_root):
        return
    _log.warning(
        "archive_target_in_site_root",
        path=str(resolved),
        site_root=str(site_root),
    )
    emit_warning(
        RunWarning(
            code="archive_target_in_site_root",
            message=f"the archive is written inside the Site Root, where the web "
            f"server may serve it: {resolved}",
            fields=MappingProxyType({"path": str(resolved)}),
        ),
        on_warning,
    )


def plan_backup(
    site: SiteDescriptor,
    scope: BackupScope,
    target: StorageTarget,
    *,
    cipher: PayloadCipher | None,
    exclusion: ExclusionPolicy,
    extra_tools: tuple[ToolRequirement, ...],
) -> BackupPlan:
    """The root's one planning path: the plan a Run and a Dry Run both execute.

    The exclusion set planned is the policy's own, the same object the writer
    is handed, so a real Run and a Dry Run plan the same set.
    """
    return build_backup_plan(
        site,
        scope,
        target,
        encrypted=cipher is not None,
        exclusion_source=exclusion.source,
        exclusion_patterns=tuple(exclusion.patterns),
        extra_tools=extra_tools,
    )


#: The line of a step whose line names no parameter.
_STEP_LINES: Final[Mapping[BackupStep, str]] = {
    BackupStep.DUMP: "dump",
    BackupStep.COLLECT_SITE_ROOT: "collect Site Root",
    BackupStep.ENCRYPT: "encrypt the payload",
    BackupStep.WRITE_ARCHIVE: "write Archive",
    BackupStep.VERIFY: "verify",
    BackupStep.PUBLISH: "publish",
}


def _collect_line(site: SiteDescriptor, plan: BackupPlan) -> str:
    patterns = plan.exclusion_patterns
    if not patterns:
        # UX-4 (BL-E002-103): say what it means, not "excluding nothing, none".
        return f"collect Site Root {site.site_root} (no exclusion patterns)"
    return (
        f"collect Site Root {site.site_root} (excluding {len(patterns)} patterns, "
        f"{plan.exclusion_source}: {', '.join(patterns)})"
    )


def plan_actions(
    plan: BackupPlan, *, site: SiteDescriptor, credential_source: str
) -> tuple[PlannedAction, ...]:
    """``plan`` as the ordered actions a Run takes, as reported and logged.

    A plan that dumps first resolves credentials and checks dump readiness.
    The collection line names the exclusion patterns and their source, never
    the paths they would exclude: nothing reads the Site Root to plan it
    (ADR-0019). No line names a database client binary (ADR-0015).
    """
    entries: list[tuple[str, str, dict[str, object]]] = []
    if BackupStep.DUMP in plan.steps:
        entries.append(
            (
                "RESOLVE_CREDENTIALS",
                f"resolve credentials from {credential_source}",
                {"credential_source": credential_source},
            )
        )
        entries.append(
            (
                "CHECK_DUMP",
                "check dump readiness",
                {"db_name": site.db_name},
            )
        )
    for step in plan.steps:
        text, fields = _STEP_LINES[step], {}
        if step is BackupStep.DUMP:
            text = f"dump {site.db_name} from {site.db_host}:{site.db_port}"
            fields = {
                "db_name": site.db_name,
                "db_host": site.db_host,
                "db_port": site.db_port,
            }
        elif step is BackupStep.COLLECT_SITE_ROOT:
            text = _collect_line(site, plan)
            fields = {
                "site_root": str(site.site_root),
                "exclusion_source": plan.exclusion_source,
                "exclusion_patterns": list(plan.exclusion_patterns),
            }
        elif step is BackupStep.PUBLISH:
            text = f"publish to {plan.target.scheme}:{plan.target.path}"
            fields = {"path": str(plan.target.path)}
        entries.append((step.value, text, fields))
    return tuple(
        PlannedAction(ordinal=n, step=step, text=f"{n}. {text}", fields=fields)
        for n, (step, text, fields) in enumerate(entries, 1)
    )


def _log_plan(actions: Sequence[PlannedAction]) -> None:
    for action in actions:
        _log.info(
            "planned_action",
            ordinal=action.ordinal,
            step=action.step,
            action=action.text,
            **action.fields,
        )


def _exclusions(
    plan: BackupPlan, excluded: tuple[ExcludedEntry, ...]
) -> ManifestExclusions:
    if plan.exclusion_source not in _EXCLUSION_SOURCES:  # a host's own policy
        msg = (
            f"exclusion policy source {plan.exclusion_source!r} is not "
            f"{_EXCLUSION_SOURCES}"
        )
        raise UsageError(msg, arguments=("exclusion",))
    return ManifestExclusions(
        source=ExclusionSource(plan.exclusion_source),
        patterns=plan.exclusion_patterns,
        excluded=excluded,
    )


def _publish(staged: StagedArchive | None) -> bool:
    """``VERIFY`` and ``PUBLISH``, both emitted from inside ``publish()``."""
    if staged is None:
        msg = "VERIFY before WRITE_ARCHIVE"
        raise UsageError(msg, arguments=())
    return staged.publish()


def execute_backup_plan(
    plan: BackupPlan,
    *,
    site: SiteDescriptor,
    config: LocatedConfig,
    workspace: Path,
    database: DatabaseCommand | None,
    store: ArchiveStoreCommand,
    writer: ArchiveWriterCommand,
    cipher: PayloadCipher | None,
    exclusion: ExclusionPolicy,
    origin: ManifestOrigin | None,
    now: datetime,
) -> BackupResult:
    """Run ``plan``'s steps in order through the command ports it is handed.

    ``DUMP`` runs inside ``phase("DUMP")``; collection, ``ENCRYPT`` and
    ``WRITE_ARCHIVE`` are the writer's, and ``VERIFY`` and ``PUBLISH`` are
    emitted from inside ``publish()``, once each. Any exception after
    ``stage()`` discards the staged Archive before it propagates.
    ``database`` is ``None`` exactly when the plan has no ``DUMP`` step: a
    ``files`` backup binds no database and resolves no credential (FR-2).
    ``exclusion`` must be the set the plan records (source and patterns), as
    ``cipher`` must match its ``ENCRYPT`` step; either mismatch is a
    ``UsageError`` before any effect.
    """
    encrypted = BackupStep.ENCRYPT in plan.steps
    if encrypted != (cipher is not None):
        msg = "the plan's ENCRYPT step and the cipher binding disagree"
        raise UsageError(msg, arguments=())
    # R8: the Manifest records the plan's set, the writer applies the policy.
    planned = (plan.exclusion_source, plan.exclusion_patterns)
    if planned != (exclusion.source, tuple(exclusion.patterns)):
        msg = "the plan's exclusion set and the exclusion policy disagree"
        raise UsageError(msg, arguments=())
    dumps = BackupStep.DUMP in plan.steps
    if dumps != (database is not None):
        msg = "the plan's DUMP step and the database binding disagree"
        raise UsageError(msg, arguments=())
    _log.info("backup_started", backup_scope=plan.scope.value)
    if cipher is not None:
        cipher.check()
    dump_path = workspace / DUMP_RELATIVE
    collects = BackupStep.COLLECT_SITE_ROOT in plan.steps
    staged: StagedArchive | None = None
    manifest: Manifest | None = None
    performed = False

    def build_manifest(
        index: tuple[ManifestMember, ...], excluded: tuple[ExcludedEntry, ...]
    ) -> Manifest:
        return Manifest(
            format_version=FORMAT_VERSION,
            created_at=created_at_text(now),
            backup_scope=plan.scope.value,
            source=source_from_descriptor(site),
            encrypted=False,
            payload=pending_payload(encrypted=False),
            members=index,
            dump_present=dumps,
            origin=origin,
            exclusions=_exclusions(plan, excluded),
            config_path=None if config.location is None else config.location.value,
        )

    try:
        for step in plan.steps:
            if step is BackupStep.DUMP:
                # Bound: the DUMP/database check above ran before any step.
                dumper = cast("DatabaseCommand", database)
                with phase(step.value) as record:
                    dumped = dumper.dump_to(site.database_location(), dump_path)
                    record.bytes = dumped.bytes
            elif step is BackupStep.WRITE_ARCHIVE:
                staged = store.stage(plan.target)
                if collects and staged.staged_path is not None:
                    exclusion = _SkipStaged(
                        exclusion, Path(site.site_root), staged.staged_path
                    )
                manifest = writer.write(
                    staged.stream,
                    site_root=Path(site.site_root) if collects else None,
                    dump=dump_path if dumps else None,
                    config=config.path,
                    exclusion=exclusion,
                    cipher=cipher,
                    manifest_builder=build_manifest,
                    workspace=workspace,
                )
            elif step is BackupStep.VERIFY:
                performed = _publish(staged)
            # COLLECT_SITE_ROOT and ENCRYPT run inside the writer, PUBLISH
            # inside publish().
    except BaseException:
        if staged is not None:
            staged.discard()
        raise
    if manifest is None:
        msg = "the plan wrote no Archive"
        raise UsageError(msg, arguments=())
    final = Path(plan.target.path)
    size = final.stat().st_size if performed else manifest.payload.size
    kinds = Counter(member.kind.value for member in manifest.members)
    _log.info(
        "backup_completed",
        path=str(final),
        format_version=manifest.format_version,
        members=dict(sorted(kinds.items())),
        bytes=size,
        performed=performed,
    )
    return BackupResult(
        path=plan.target.path,
        scope=plan.scope,
        bytes=size,
        encrypted=encrypted,
        # Keyed on what the store reported, not on the selection: a publish
        # that happened passed the gate's verification of the v1 the writer
        # wrote; one that did not (the no-op store) verified nothing.
        integrity=Integrity.VERIFIED if performed else None,
        performed=performed,
        plan=plan,
    )


def storage_target_from_cli(text: str, *, argument: str = "--archive") -> StorageTarget:
    """The Storage Target ``--archive`` names, never silently rewritten.

    ``argument`` is what a refusal names: the flag, or the library caller's
    parameter (``archive``, ``target``; BL-E006-015).

    A bare path is taken as given — ``#``, ``?`` and ``:`` are file-name
    characters there. Only text whose scheme is followed by ``/`` is a URL
    (``file:/x``, ``file:///x``), so a relative name such as
    ``site-2026T10:00.tar.gz`` stays a path (R-04). A URL's path is
    percent-decoded, and a host other than ``localhost``, a query or a fragment
    is refused rather than dropped.
    """
    parts = urlsplit(text)
    if not parts.scheme or not text[len(parts.scheme) + 1 :].startswith("/"):
        target = parse_storage_target("", text)
    else:
        if parts.netloc not in _LOCAL_HOSTS or parts.query or parts.fragment:
            msg = f"--archive {text!r}: a host, query or fragment is not supported"
            raise UsageError(msg, arguments=(argument,))
        target = parse_storage_target(parts.scheme, unquote(parts.path))
    if "\x00" in str(target.path):
        msg = f"--archive {text!r}: a path may not contain a NUL"
        raise UsageError(msg, arguments=(argument,))
    return dataclasses.replace(target, path=PurePosixPath(Path(target.path).absolute()))


def backup(
    site: SiteDescriptor,
    scope: BackupScope,
    target: StorageTarget,
    *,
    ctx: RunContext,
    config: LocatedConfig,
    cipher: PayloadCipher | None,
    exclusion: ExclusionPolicy,
    origin: ManifestOrigin | None,
    extra_tools: tuple[ToolRequirement, ...] = (),
    preflight: Preflight | None = None,
) -> BackupResult:
    """Back ``site`` up to ``target`` inside ``ctx``; never opens a Run.

    Every keyword without a default is required: a Safety Snapshot states its
    encryption, exclusion and origin, never inherits them by omission. The
    final-name check and preflight (AD-13) — both queries — run before the
    first command adapter is called. ``preflight=None`` binds the roots' one
    ``RootPreflight`` (``cli.preflight.root_preflight``).
    """
    plan = plan_backup(
        site, scope, target, cipher=cipher, exclusion=exclusion, extra_tools=extra_tools
    )
    adapters = ctx.adapters
    if adapters.archive_store_query.exists(target):
        raise ArchiveExistsError(target.path)
    checker = preflight or root_preflight(
        ctx, site, credential_source=site.credential_source
    )
    require_tools(checker, plan.required_tools)
    return execute_backup_plan(
        plan,
        site=site,
        config=config,
        workspace=ctx.workspace,
        database=adapters.database_command if BackupStep.DUMP in plan.steps else None,
        store=adapters.archive_store_command,
        writer=adapters.archive_writer,
        cipher=cipher,
        exclusion=exclusion,
        origin=origin,
        now=ctx.clock.now(),
    )


def drive_backup(
    request: BackupRequest,
    *,
    ctx: RunContext | None,
    tracer: Tracer | None = None,
    secrets: SecretRegistry | None = None,
    command_args: Sequence[str] | None = None,
    on_plan: Callable[[Sequence[PlannedAction]], None] | None = None,
    site_lock: SiteLock | None = None,
    on_warning: WarningSink | None = None,
    environ: Mapping[str, str] = NO_ENVIRONMENT,
) -> BackupResult:
    """The backup driver: returns or raises ``BackupError``; never prints.

    ``environ`` is what the credential builders read (``L3IO_WP_BACKUP_DB_*``),
    as ``drive_verify`` takes it: the CLI root passes ``os.environ``, the
    library ``backup`` too, and no driver reads ``os.environ`` itself
    (owner decision 2026-11-07, BL-E006-013).

    Its operator notices (``RunWarning``) go to ``on_warning`` when given,
    else to stderr (``cli/output.emit_warning``), as ``drive_restore``'s do
    (owner decision 2026-11-07, BL-E008-012).

    The Site Lock for the canonical Site Root is held for the whole Run
    (``site_lock=None`` binds ``cli.adapter_sets.real_site_lock()``); a Run
    already holding it is joined without acquiring it again.

    The planned actions are logged, and handed to ``on_plan``, before any
    check or effect, so a Run refused by a query has reported what it
    planned. A failure before planning (the Site Descriptor, the credential
    source) has planned nothing and reports nothing.

    A process running as root is refused before anything is read
    (``RootBackupRefusedError``, owner decision 68): the library never
    switches its host's user; the CLI root has switched by now.
    """
    privileges.require_not_root(request.site_root, operation="backup")
    try:
        site_root = Path(request.site_root).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SiteRootNotFoundError(str(Path(request.site_root).absolute())) from exc
    with join_or_open(
        ctx,
        operation="backup",
        tracer=tracer,
        secrets=secrets,
        command_args=command_args,
        dry_run=request.dry_run,
        site_root=site_root,
        site_lock=real_site_lock() if site_lock is None else site_lock,
    ) as handle:
        run = handle.ctx
        config = locate_wp_config(site_root, stated=request.wp_config)
        site, wp_config = site_descriptor_from_wp_config(
            site_root, config_file=config.path, environ=environ, secrets=run.secrets
        )
        run.run_span.set_attributes(db_name=site.db_name)
        target, chosen_by, create = _target(request, site_root, site, run)
        _log.info("archive_target_chosen", path=str(target.path), chosen_by=chosen_by)
        _warn_target_inside(target, site_root, on_warning=on_warning)
        if request.scope is not BackupScope.FILES:
            _log_database_host(site)
            check_db_tls(request.db_tls)
        credentials = wordpress_source(
            request.credentials,
            wp_config=wp_config,
            environ=environ,
            secrets=run.secrets,
        )
        plan = plan_backup(
            site,
            request.scope,
            target,
            cipher=request.cipher,
            exclusion=request.exclusion,
            extra_tools=request.extra_tools,
        )
        actions = plan_actions(plan, site=site, credential_source=credentials.source)
        _log_plan(actions)
        if on_plan is not None:
            on_plan(actions)
        # Only a plan that dumps touches the credentials (FR-2, AD-9), and
        # only wp-config.php is checked up front (FR-1).
        if BackupStep.DUMP in plan.steps and request.credentials is None:
            require_wp_config_credentials(credentials, environ=environ)
        if handle.opened:
            # AD-4: the one place a Dry Run is selected — by adapter set.
            real = real_adapters(
                wordpress_source=credentials,
                secrets=run.secrets,
                tracer=run.tracer,
                db_tls=request.db_tls,
                create_archive_directory=create,
            )
            run.bind_adapters(dry_run_adapters(real) if request.dry_run else real)
        result = backup(
            site,
            request.scope,
            target,
            ctx=run,
            config=config,
            cipher=request.cipher,
            exclusion=request.exclusion,
            origin=None,
            extra_tools=request.extra_tools,
            preflight=root_preflight(run, site, credential_source=credentials.source),
        )
        run.run_span.set_attributes(archive_bytes=result.bytes)
        return dataclasses.replace(
            result, actions=tuple(action.text for action in actions)
        )


def _target(
    request: BackupRequest, site_root: Path, site: SiteDescriptor, run: RunContext
) -> tuple[StorageTarget, str, bool]:
    """The Storage Target, what chose it, and whether its directory may be made.

    Only the per-user data directory is created when missing (``0700``, by the
    store's ``stage``, which a Dry Run replaces); a directory the operator
    named must exist.
    """
    if request.target is not None:
        return request.target, request.target_chosen_by, False
    site_name = site.db_name if request.site_name is None else request.site_name
    name = default_archive_name(site_name, run.clock.now())
    directory, chosen_by = _archive_directory(request, site_root)
    target = storage_target_from_cli(str(directory / name))
    return target, chosen_by, chosen_by == _DATA_DIRECTORY


def _archive_directory(request: BackupRequest, site_root: Path) -> tuple[Path, str]:
    """The directory the Archive is written into, and what chose it."""
    if request.target is not None:
        return Path(request.target.path).parent, request.target_chosen_by
    if request.archive_dir is not None:
        return Path(request.archive_dir), "--archive-dir"
    return default_archive_directory(site_root)


def _read_as_root(
    request: BackupRequest, *, environ: Mapping[str, str], secrets: SecretRegistry
) -> BackupRequest:
    """Owner decision 67: the database credentials, resolved while still root.

    Only a Run that dumps reads credentials (FR-2). The whole WordPress chain
    is resolved (the spec, the environment, ``wp-config.php``) into a
    ``ResolvedCredentialSpec`` the Run uses as it is, so no credential
    source (an AWS profile in root's home, say) is consulted after the
    switch. ``wp-config.php`` itself is read again by the Run, as the owner:
    its other settings and its copy in the Archive. The passphrase was
    resolved before this.
    """
    if request.scope is BackupScope.FILES:
        return request
    try:
        site_root = Path(request.site_root).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SiteRootNotFoundError(str(Path(request.site_root).absolute())) from exc
    config = locate_wp_config(site_root, stated=request.wp_config)
    _site, wp_config = site_descriptor_from_wp_config(
        site_root, config_file=config.path, environ=environ, secrets=secrets
    )
    source = wordpress_source(
        request.credentials, wp_config=wp_config, environ=environ, secrets=secrets
    )
    resolved = resolve_wordpress_now(source, environ=environ)
    return dataclasses.replace(request, credentials=resolved)


def _cipher(
    *,
    encrypt: bool,
    environ: Mapping[str, str],
    secrets: SecretRegistry,
    explicit: Secret | None = None,
    explicit_source: str | None = None,
) -> PassphraseCipher | None:
    """The cipher the flags ask for, its passphrase resolved into ``secrets``.

    Without ``--encrypt-with-passphrase`` the AD-26 chain is not consulted and
    nothing is prompted: a set variable is noted. With it, the passphrase must
    resolve and meet the encryption floor, or the root refuses before anything
    is read (``require_passphrase``).
    """
    if not encrypt:
        note_unused_passphrase(environ, explicit_source=explicit_source)
        return None
    resolved = require_passphrase(
        explicit=explicit,
        environ=environ,
        prompt=terminal_prompt(),
        secrets=secrets,
        explicit_source=explicit_source,
    )
    return build_payload_cipher(passphrase=resolved.value, secrets=secrets)


def _exclusion(
    exclude: Sequence[str] | None, *, no_default_excludes: bool
) -> ExclusionPolicy | None:
    """The set ``--exclude``/``--no-default-excludes`` ask for; ``None`` for none.

    ``None`` leaves the request's default (:func:`default_exclusion_policy`).
    An invalid pattern is ``InvalidExclusionPatternError`` (``USAGE``).
    """
    patterns = tuple(exclude or ())
    if not patterns and not no_default_excludes:
        return None
    return LoggedExclusionPolicy(
        ExclusionSet.from_patterns(patterns, include_defaults=not no_default_excludes)
    )


def _refuse_flags_with_list(
    values: Mapping[str, object], typed: frozenset[str] | None = None
) -> None:
    """``UsageError``: ``--list-default-excludes`` takes no other backup flag.

    It prints and exits without a Run, so any other flag would be accepted
    and silently ignored (E008 closure A-02). ``--wp-dir`` is optional with it
    (BL-E008-013, owner decision 2026-11-07) and the one flag it may carry. The
    flags checked are the backup parser's own, so a flag added later is
    covered without touching this function. ``typed`` is the settings given
    on the command line (owner decision 60): one set by a variable or a
    config file is a standing setting, not a flag given with this one, and
    is not refused.
    """
    from l3io.wp.backup.cli.parser import BACKUP_COMMAND, build_parser

    subparsers = next(
        action.choices
        for action in build_parser()._actions  # noqa: SLF001
        if isinstance(action, argparse._SubParsersAction)  # noqa: SLF001
    )
    exempt = {"help", "wp_dir", "list_default_excludes", "config"}

    def unset(action: argparse.Action) -> object:
        # A SUPPRESS default (``--whatIf``) leaves the top-level value: False.
        return False if action.default == argparse.SUPPRESS else action.default

    def given_here(action: argparse.Action) -> bool:
        if typed is not None and action.dest in SETTING_NAMES:
            return action.dest in typed
        return values.get(action.dest, unset(action)) != unset(action)

    given = tuple(
        action.option_strings[-1]
        for action in subparsers[BACKUP_COMMAND]._actions  # noqa: SLF001
        if action.option_strings and action.dest not in exempt and given_here(action)
    )
    if given:
        message = (
            "--list-default-excludes takes no other backup flag besides "
            f"--wp-dir; remove {', '.join(given)}"
        )
        raise UsageError(message, arguments=given)


#: The origins a credential pair's environment step reads for itself: neither
#: is turned into an explicit spec (``adapters/credential_providers``).
_CHAIN_ORIGINS: Final = frozenset({"environment", "default", None})


def _explicit_pair(
    user: str | None, password: Secret | str | None, origins: Mapping[str, str]
) -> tuple[str | None, Secret | str | None]:
    """``--db-user``/``--db-password`` as an explicit pair, when either was typed
    or came from the config file; both from the environment (or neither) are
    the credential chain's own environment step, read there (owner decision 60).
    """
    if all(origins.get(name) in _CHAIN_ORIGINS for name in ("db_user", "db_password")):
        return None, None
    return user, password


def _chosen_by(origin: str | None) -> str:
    """What chose ``--archive``'s value, for ``archive_target_chosen``."""
    if origin == "environment":
        return env_name("archive")
    if origin == "config file":
        return f"the config file's {config_key('archive')}"
    return "--archive"


def run_backup(args: argparse.Namespace, *, secrets: SecretRegistry) -> ExitCode:
    """``wpbackup2 backup``: ``ExitCode.OK``, or raises ``BackupError``.

    ``secrets`` is the root's registry; the Run adopts it, and the root
    reports a failure through it (ADR-0018). A ``--whatIf`` Run that is
    refused prints the actions it had planned on stdout before the failure
    propagates, so its plan does not depend on ``--log-level``.
    ``--list-default-excludes`` prints the default set, one pattern per line,
    and returns ``OK`` with no Run and nothing read.

    Started as root (owner decision 67): a root-owned Site Root is refused
    first; the passphrase and the database credentials are read; then the
    process switches to the Site Root's owner (``cli/privileges``) and an
    Archive directory that owner cannot write is refused, all before the Run
    opens.
    """
    origins = setting_origins(vars(args))
    if args.list_default_excludes:
        _refuse_flags_with_list(vars(args), typed=typed_settings(vars(args)))
        emit_result("\n".join(enumerate_defaults()))
        return ExitCode.OK
    # Owner decision 67: a root-owned Site Root is refused before anything
    # else is read.
    owner = privileges.owner_to_run_as(Path(args.wp_dir), operation="backup")
    exclusion = _exclusion(
        args.exclude, no_default_excludes=bool(args.no_default_excludes)
    )
    passphrase, passphrase_source = explicit_secret(
        args.passphrase, origins.get("passphrase"), name="passphrase"
    )
    cipher = _cipher(
        encrypt=bool(args.encrypt_with_passphrase),
        environ=os.environ,
        secrets=secrets,
        explicit=passphrase,
        explicit_source=passphrase_source,
    )
    extra_tools = () if cipher is None else cipher.required_tools()
    if args.archive == LATEST:
        msg = (
            "--archive latest names an existing archive to restore; a backup "
            "writes a new one (give a path, or --archive-dir)"
        )
        raise UsageError(msg, arguments=("archive",))
    db_user, db_password = _explicit_pair(args.db_user, args.db_password, origins)
    request = BackupRequest(
        site_root=Path(args.wp_dir),
        scope=backup_scope_from_cli(args.mode),
        target=None if not args.archive else storage_target_from_cli(args.archive),
        wp_config=None if args.wp_config is None else Path(args.wp_config),
        credentials=wordpress_spec(
            aws_secret_id=args.db_credentials_aws_secret_id,
            aws_region=args.db_credentials_aws_region,
            username=db_user,
            password=db_password,
        ),
        cipher=cipher,
        exclusion=default_exclusion_policy() if exclusion is None else exclusion,
        dry_run=bool(args.what_if),
        extra_tools=extra_tools,
        archive_dir=None if args.archive_dir is None else Path(args.archive_dir),
        db_tls=db_tls_setting(args.db_tls, args.db_tls_ca),
        site_name=args.site_name,
        target_chosen_by=_chosen_by(origins.get("archive")),
    )
    if origins.get("db_password") == "command line":
        note_password_on_command_line("--db-password")
    if owner is not None:
        # Owner decision 67: everything only root may read, then the switch.
        request = _read_as_root(request, environ=os.environ, secrets=secrets)
        privileges.switch_to(owner, environ=os.environ, operation="backup")
        site_root = Path(request.site_root).resolve()
        privileges.require_writable(_archive_directory(request, site_root)[0], owner)
    planned: list[str] = []
    try:
        result = drive_backup(
            request,
            ctx=None,
            secrets=secrets,
            environ=os.environ,
            on_plan=lambda actions: planned.extend(a.text for a in actions),
        )
    except BaseException:
        # A refused Dry Run reports what it planned (FR-19); a refused real
        # Run's stdout stays empty. The composition root may name it (AD-4).
        if request.dry_run and planned:
            emit_result("\n".join(planned))
        raise
    # A Dry Run published nothing: its result is the ordered plan (FR-19),
    # then a line that says so (UX-2, BL-E002-102).
    if result.performed:
        emit_result(str(result.path))
    else:
        emit_result("\n".join([*result.actions, DRY_RUN_TRAILER]))
    return ExitCode.OK
