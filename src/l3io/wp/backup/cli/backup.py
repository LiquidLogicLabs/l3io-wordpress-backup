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
"""

from __future__ import annotations

import argparse
import dataclasses
import os
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Final, Literal, cast
from urllib.parse import unquote, urlsplit

import structlog

from l3io.wp.backup.adapters.crypto import PassphraseCipher, build_payload_cipher
from l3io.wp.backup.adapters.database import (
    LocatedConfig,
    locate_wp_config,
    require_wp_config_credentials,
    site_descriptor_from_wp_config,
)
from l3io.wp.backup.adapters.exclusions import LoggedExclusionPolicy
from l3io.wp.backup.adapters.phase import phase
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.cli.adapter_sets import (
    dry_run_adapters,
    real_adapters,
    real_site_lock,
)
from l3io.wp.backup.cli.context import RunContext, join_or_open
from l3io.wp.backup.cli.credentials import wordpress_source, wordpress_spec
from l3io.wp.backup.cli.output import emit_result
from l3io.wp.backup.cli.passphrase import (
    note_unused_passphrase,
    require_passphrase,
    terminal_prompt,
)
from l3io.wp.backup.cli.preflight import require_tools, root_preflight
from l3io.wp.backup.cli.scope import backup_scope_from_cli
from l3io.wp.backup.core.backup import BackupPlan, BackupStep, build_backup_plan
from l3io.wp.backup.core.exclusions import ExclusionSet, enumerate_defaults
from l3io.wp.backup.domain.archive_format import (
    DUMP_RELATIVE,
    FORMAT_VERSION,
    PAYLOAD_MEMBER,
    Integrity,
    Manifest,
    ManifestExclusions,
    ManifestMember,
    ManifestOrigin,
    ManifestPayload,
    source_from_descriptor,
)
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.credentials import CredentialSpec
from l3io.wp.backup.domain.errors import (
    ArchiveExistsError,
    InvalidSiteDescriptorError,
    UsageError,
)
from l3io.wp.backup.domain.exclusion import ExcludedEntry, ExclusionDecision
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.domain.preflight import ToolRequirement
from l3io.wp.backup.domain.site import SiteDescriptor
from l3io.wp.backup.domain.storage_target import (
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
from l3io.wp.backup.ports.database import DatabaseCommand
from l3io.wp.backup.ports.exclusion import ExclusionPolicy
from l3io.wp.backup.ports.lock import SiteLock
from l3io.wp.backup.ports.preflight import Preflight
from l3io.wp.backup.ports.tracing import Tracer

__all__ = [
    "BackupRequest",
    "BackupResult",
    "PlannedAction",
    "backup",
    "default_exclusion_policy",
    "drive_backup",
    "execute_backup_plan",
    "plan_actions",
    "plan_backup",
    "run_backup",
    "storage_target_from_cli",
]

_log = structlog.get_logger(__name__)

_EXCLUSION_SOURCES: Final = ("default", "custom", "none")
_LOCAL_HOSTS: Final = ("", "localhost")
#: Replaced by the writer with the payload it actually wrote.
_PENDING_PAYLOAD: Final = ManifestPayload(name=PAYLOAD_MEMBER, size=0, sha256="0" * 64)


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


@dataclass(frozen=True, slots=True)
class _SkipStaged:
    """An exclusion policy that also leaves out the Archive being written.

    The staged file is created before the Site Root is collected; with a
    target inside the Site Root (the default is the current directory) it
    would otherwise be collected into itself.
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
        if not is_dir and self.site_root / relative == self.staged:
            return ExclusionDecision(
                excluded=True, pattern="the Archive being written", protected=False
            )
        return self.inner.decide(relative, is_dir=is_dir)


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
    ``target=None`` means the default Archive name in the current directory.
    ``dry_run`` selects the no-op command adapters (AD-4) when the driver
    opens the Run; ``extra_tools`` reaches preflight through the plan.
    """

    site_root: Path
    scope: BackupScope
    target: StorageTarget | None
    wp_config: Path | None
    credentials: CredentialSpec | None
    cipher: PayloadCipher | None
    exclusion: ExclusionPolicy = field(default_factory=default_exclusion_policy)
    dry_run: bool = False
    extra_tools: tuple[ToolRequirement, ...] = ()


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
        source = plan.exclusion_source
        return f"collect Site Root {site.site_root} (excluding nothing, {source})"
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
                "check dump readiness (DatabaseQuery.check_dump)",
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
    if plan.exclusion_source not in _EXCLUSION_SOURCES:
        msg = (
            f"exclusion policy source {plan.exclusion_source!r} is not "
            f"{_EXCLUSION_SOURCES}"
        )
        raise UsageError(msg, arguments=("exclusion",))
    return ManifestExclusions(
        source=cast("Literal['default', 'custom', 'none']", plan.exclusion_source),
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
    """
    encrypted = BackupStep.ENCRYPT in plan.steps
    if encrypted != (cipher is not None):
        msg = "the plan's ENCRYPT step and the cipher binding disagree"
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
            created_at=now.astimezone(UTC).isoformat(),
            backup_scope=plan.scope.value,
            source=source_from_descriptor(site),
            encrypted=False,
            payload=_PENDING_PAYLOAD,
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


def storage_target_from_cli(text: str) -> StorageTarget:
    """The Storage Target ``--archive`` names, never silently rewritten.

    A bare path is taken as given — ``#`` and ``?`` are file-name characters
    there. Only text with a scheme is a URL: its path is percent-decoded, and a
    host other than ``localhost``, a query or a fragment is refused rather than
    dropped.
    """
    parts = urlsplit(text)
    if not parts.scheme:
        target = parse_storage_target("", text)
    else:
        if parts.netloc not in _LOCAL_HOSTS or parts.query or parts.fragment:
            msg = f"--archive {text!r}: a host, query or fragment is not supported"
            raise UsageError(msg, arguments=("--archive",))
        target = parse_storage_target(parts.scheme, unquote(parts.path))
    if "\x00" in str(target.path):
        msg = f"--archive {text!r}: a path may not contain a NUL"
        raise UsageError(msg, arguments=("--archive",))
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
) -> BackupResult:
    """The backup driver: returns or raises ``BackupError``; never prints.

    The Site Lock for the canonical Site Root is held for the whole Run
    (``site_lock=None`` binds ``cli.adapter_sets.real_site_lock()``); a Run
    already holding it is joined without acquiring it again.

    The planned actions are logged, and handed to ``on_plan``, before any
    check or effect, so a Run refused by a query has reported what it
    planned. A failure before planning (the Site Descriptor, the credential
    source) has planned nothing and reports nothing.
    """
    try:
        site_root = Path(request.site_root).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        msg = "site root does not exist"
        raise InvalidSiteDescriptorError(msg, paths=(str(request.site_root),)) from exc
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
            site_root, config_file=config.path
        )
        run.run_span.set_attributes(db_name=site.db_name)
        target = request.target or storage_target_from_cli(
            default_archive_name(site.db_name, run.clock.now())
        )
        credentials = wordpress_source(
            request.credentials,
            wp_config=wp_config,
            environ=os.environ,
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
            require_wp_config_credentials(credentials)
        if handle.opened:
            # AD-4: the one place a Dry Run is selected — by adapter set.
            real = real_adapters(
                wordpress_source=credentials,
                secrets=run.secrets,
                tracer=run.tracer,
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


def _cipher(
    *, encrypt: bool, environ: Mapping[str, str], secrets: SecretRegistry
) -> PassphraseCipher | None:
    """The cipher the flags ask for, its passphrase resolved into ``secrets``.

    Without ``--encrypt-with-passphrase`` the AD-26 chain is not consulted and
    nothing is prompted: a set variable is noted. With it, the passphrase must
    resolve and meet the encryption floor, or the root refuses before anything
    is read (``require_passphrase``).
    """
    if not encrypt:
        note_unused_passphrase(environ)
        return None
    resolved = require_passphrase(
        explicit=None, environ=environ, prompt=terminal_prompt(), secrets=secrets
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


def run_backup(args: argparse.Namespace, *, secrets: SecretRegistry) -> ExitCode:
    """``wpbackup2 backup``: ``ExitCode.OK``, or raises ``BackupError``.

    ``secrets`` is the root's registry; the Run adopts it, and the root
    reports a failure through it (ADR-0018). A ``--whatIf`` Run that is
    refused prints the actions it had planned on stdout before the failure
    propagates, so its plan does not depend on ``--log-level``.
    ``--list-default-excludes`` prints the default set, one pattern per line,
    and returns ``OK`` with no Run and nothing read.
    """
    if args.list_default_excludes:
        emit_result("\n".join(enumerate_defaults()))
        return ExitCode.OK
    exclusion = _exclusion(
        args.exclude, no_default_excludes=bool(args.no_default_excludes)
    )
    cipher = _cipher(
        encrypt=bool(args.encrypt_with_passphrase), environ=os.environ, secrets=secrets
    )
    extra_tools = () if cipher is None else cipher.required_tools()
    request = BackupRequest(
        site_root=Path(args.wp_dir),
        scope=backup_scope_from_cli(args.mode),
        target=None if not args.archive else storage_target_from_cli(args.archive),
        wp_config=None if args.wp_config is None else Path(args.wp_config),
        credentials=wordpress_spec(
            aws_secret_id=args.db_credentials_aws_secret_id,
            aws_region=args.db_credentials_aws_region,
        ),
        cipher=cipher,
        exclusion=default_exclusion_policy() if exclusion is None else exclusion,
        dry_run=bool(getattr(args, "what_if", False)),
        extra_tools=extra_tools,
    )
    planned: list[str] = []
    try:
        result = drive_backup(
            request,
            ctx=None,
            secrets=secrets,
            on_plan=lambda actions: planned.extend(a.text for a in actions),
        )
    except BaseException:
        # A refused Dry Run reports what it planned (FR-19); a refused real
        # Run's stdout stays empty. The composition root may name it (AD-4).
        if request.dry_run and planned:
            emit_result("\n".join(planned))
        raise
    # A Dry Run published nothing: its result is the ordered plan (FR-19).
    emit_result(str(result.path) if result.performed else "\n".join(result.actions))
    return ExitCode.OK
