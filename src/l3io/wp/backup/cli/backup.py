"""AD-1/AD-16/AD-20 — the backup composition root, its driver and the plan executor.

Three layers, each calling only the next:

- :func:`run_backup` (the CLI root) takes the Run's ``SecretRegistry`` from
  ``cli.main``, translates the flags into a :class:`BackupRequest` (the
  passphrase and cipher resolved into that registry first), makes **one**
  call to :func:`drive_backup`, renders what it returns on stdout and returns
  ``ExitCode.OK``; a failure is raised for the root to map and report,
  redacted, on stderr (FR-21, ADR-0018).
- :func:`drive_backup` (the argument-free driver the CLI and Epic 6's ``api/``
  share) canonicalises the Site Root, opens or joins the Run
  (``cli.context.join_or_open``), reads the Site Descriptor, builds the
  WordPress credential source through ``cli/credentials.py``, binds
  ``real_adapters`` only when it opened the Run, and calls :func:`backup`.
  It never prints.
- :func:`backup` (the Run-Context-accepting entry point the Safety Snapshot
  calls, E003-S02-003) runs :func:`execute_backup_plan` inside ``ctx`` with its
  workspace, adapters and clock, and never opens a Run.

:func:`execute_backup_plan` performs each step of a
:class:`~l3io.wp.backup.core.backup.BackupPlan` in order, each inside
``phase(step)``. The Archive is written into a staged file beside its final
name and becomes visible only through the store's ``publish()``, after the
verifier passes (AD-20); any failure before that discards the staged file.

Encryption binds ``None`` until
E008-S01-001 binds Epic 4's cipher.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Final, Literal, cast
from urllib.parse import unquote, urlsplit

import structlog

from l3io.wp.backup.adapters.database import (
    LocatedConfig,
    locate_wp_config,
    require_wp_config_credentials,
    site_descriptor_from_wp_config,
)
from l3io.wp.backup.adapters.phase import phase
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.adapters.tar import (
    ArchiveMemberSource,
    SiteRootCollection,
    collect_site_root,
    file_member,
    write_archive,
)
from l3io.wp.backup.cli.adapter_sets import real_adapters
from l3io.wp.backup.cli.context import RunContext, join_or_open
from l3io.wp.backup.cli.credentials import wordpress_source, wordpress_spec
from l3io.wp.backup.cli.output import emit_result
from l3io.wp.backup.cli.passphrase import note_unused_passphrase
from l3io.wp.backup.cli.scope import backup_scope_from_cli
from l3io.wp.backup.core.backup import BackupPlan, BackupStep, build_backup_plan
from l3io.wp.backup.domain.archive_format import (
    CONFIG_RELATIVE,
    DUMP_RELATIVE,
    FORMAT_VERSION,
    PAYLOAD_MEMBER,
    Integrity,
    Manifest,
    ManifestExclusions,
    ManifestMember,
    ManifestOrigin,
    ManifestPayload,
    MemberRole,
    source_from_descriptor,
)
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.credentials import CredentialSpec
from l3io.wp.backup.domain.errors import (
    ArchiveExistsError,
    InvalidSiteDescriptorError,
    UsageError,
)
from l3io.wp.backup.domain.exclusion import INCLUDE_EVERYTHING
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.domain.site import SiteDescriptor
from l3io.wp.backup.domain.storage_target import (
    StorageTarget,
    default_archive_name,
    parse_storage_target,
)
from l3io.wp.backup.ports.archive import ArchiveStoreCommand, StagedArchive
from l3io.wp.backup.ports.crypto import PayloadCipher
from l3io.wp.backup.ports.database import DatabaseCommand
from l3io.wp.backup.ports.exclusion import ExclusionPolicy
from l3io.wp.backup.ports.tracing import Tracer

__all__ = [
    "BackupRequest",
    "BackupResult",
    "backup",
    "default_exclusion_policy",
    "drive_backup",
    "execute_backup_plan",
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

    ``integrity`` is what the publish gate's verification proved;
    ``performed`` is the value ``StagedArchive.publish()`` returned — ``False``
    only from the no-op store, so a caller knows a Dry Run wrote nothing.
    """

    path: PurePosixPath
    scope: BackupScope
    bytes: int
    encrypted: bool
    integrity: Integrity
    performed: bool


def default_exclusion_policy() -> ExclusionPolicy:
    """The one place the default exclusion set is resolved (AD-26 reasoning).

    The CLI with no exclusion flag and a library :class:`BackupRequest` built
    without ``exclusion`` both land here. ``INCLUDE_EVERYTHING`` until
    E008-S01-001 binds Epic 4's ``ExclusionSet.default()``.
    """
    return INCLUDE_EVERYTHING


@dataclass(frozen=True, slots=True)
class BackupRequest:
    """Everything a backup needs from its caller; :func:`drive_backup` does the rest.

    ``scope`` has no default (AD-5). ``credentials`` is the backup-owned spec,
    never a database type (ADR-0015); ``None`` means ``wp-config.php``.
    ``target=None`` means the default Archive name in the current directory.
    """

    site_root: Path
    scope: BackupScope
    target: StorageTarget | None
    wp_config: Path | None
    credentials: CredentialSpec | None
    cipher: PayloadCipher | None
    exclusion: ExclusionPolicy = field(default_factory=default_exclusion_policy)


def _exclusions(
    policy: ExclusionPolicy, collection: SiteRootCollection | None
) -> ManifestExclusions:
    if policy.source not in _EXCLUSION_SOURCES:
        msg = f"exclusion policy source {policy.source!r} is not {_EXCLUSION_SOURCES}"
        raise UsageError(msg)
    return ManifestExclusions(
        source=cast("Literal['default', 'custom', 'none']", policy.source),
        patterns=tuple(policy.patterns),
        excluded=() if collection is None else collection.excluded,
    )


def _publish(staged: StagedArchive | None) -> bool:
    """``VERIFY`` and ``PUBLISH``, both emitted from inside ``publish()``."""
    if staged is None:
        msg = "VERIFY before WRITE_ARCHIVE"
        raise UsageError(msg)
    return staged.publish()


def execute_backup_plan(
    plan: BackupPlan,
    *,
    site: SiteDescriptor,
    config: LocatedConfig,
    workspace: Path,
    database: DatabaseCommand | None,
    store: ArchiveStoreCommand,
    cipher: PayloadCipher | None,
    exclusion: ExclusionPolicy,
    origin: ManifestOrigin | None,
    now: datetime,
) -> BackupResult:
    """Run ``plan``'s steps in order, each inside ``phase(step)``.

    ``ENCRYPT`` is emitted from inside the writer, and ``VERIFY`` and
    ``PUBLISH`` from inside ``publish()``, once each. Any exception after
    ``stage()`` discards the staged Archive before it propagates.
    ``database`` is ``None`` exactly when the plan has no ``DUMP`` step: a
    ``files`` backup binds no database and resolves no credential (FR-2).
    """
    encrypted = BackupStep.ENCRYPT in plan.steps
    if encrypted != (cipher is not None):
        msg = "the plan's ENCRYPT step and the cipher binding disagree"
        raise UsageError(msg)
    if (BackupStep.DUMP in plan.steps) != (database is not None):
        msg = "the plan's DUMP step and the database binding disagree"
        raise UsageError(msg)
    _log.info("backup_started", backup_scope=plan.scope.value)
    if cipher is not None:
        cipher.check()
    dump_path = workspace / DUMP_RELATIVE
    members: list[ArchiveMemberSource] = []
    collection: SiteRootCollection | None = None
    staged: StagedArchive | None = None
    manifest: Manifest | None = None
    performed = False

    def build_manifest(index: tuple[ManifestMember, ...]) -> Manifest:
        return Manifest(
            format_version=FORMAT_VERSION,
            created_at=now.astimezone(UTC).isoformat(),
            backup_scope=plan.scope.value,
            source=source_from_descriptor(site),
            encrypted=False,
            payload=_PENDING_PAYLOAD,
            members=index,
            dump_present=BackupStep.DUMP in plan.steps,
            origin=origin,
            exclusions=_exclusions(exclusion, collection),
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
                members.append(file_member(MemberRole.DUMP, DUMP_RELATIVE, dump_path))
            elif step is BackupStep.COLLECT_SITE_ROOT:
                with phase(step.value) as record:
                    collection = collect_site_root(Path(site.site_root), exclusion)
                    record.bytes = sum(m.size for m in collection.members)
            elif step is BackupStep.WRITE_ARCHIVE:
                members.append(
                    file_member(MemberRole.CONFIG, CONFIG_RELATIVE, config.path)
                )
                if collection is not None:
                    members.extend(collection.members)
                staged = store.stage(plan.target)
                with phase(step.value) as record:
                    manifest = write_archive(
                        staged.stream,
                        build_manifest,
                        members,
                        workspace=workspace,
                        cipher=cipher,
                    )
                    record.bytes = manifest.payload.size
            elif step is BackupStep.VERIFY:
                performed = _publish(staged)
            # ENCRYPT runs inside write_archive and PUBLISH inside publish().
    except BaseException:
        if staged is not None:
            staged.discard()
        raise
    if manifest is None:
        msg = "the plan wrote no Archive"
        raise UsageError(msg)
    final = Path(plan.target.path)
    size = final.stat().st_size if performed else manifest.payload.size
    kinds = Counter(member.kind.value for member in manifest.members)
    _log.info(
        "backup_completed",
        path=str(final),
        format_version=manifest.format_version,
        members=dict(sorted(kinds.items())),
        bytes=size,
    )
    return BackupResult(
        path=plan.target.path,
        scope=plan.scope,
        bytes=size,
        encrypted=encrypted,
        # The publish gate raises unless verification passed, and the writer
        # only writes v1, whose checksums it checked.
        integrity=Integrity.VERIFIED,
        performed=performed,
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
            raise UsageError(msg)
        target = parse_storage_target(parts.scheme, unquote(parts.path))
    if "\x00" in str(target.path):
        msg = f"--archive {text!r}: a path may not contain a NUL"
        raise UsageError(msg)
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
) -> BackupResult:
    """Back ``site`` up to ``target`` inside ``ctx``; never opens a Run.

    Every keyword is required: a Safety Snapshot states its encryption,
    exclusion and origin, never inherits them by omission.
    """
    plan = build_backup_plan(site, scope, target, encrypted=cipher is not None)
    adapters = ctx.adapters
    if adapters.archive_store_query.exists(target):
        raise ArchiveExistsError(target.path)
    dumps = BackupStep.DUMP in plan.steps
    return execute_backup_plan(
        plan,
        site=site,
        config=config,
        workspace=ctx.workspace,
        database=adapters.database_command if dumps else None,
        store=adapters.archive_store_command,
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
) -> BackupResult:
    """The backup driver: returns or raises ``BackupError``; never prints."""
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
        plan = build_backup_plan(
            site, request.scope, target, encrypted=request.cipher is not None
        )
        # Only a plan that dumps touches the credentials (FR-2, AD-9), and
        # only wp-config.php is checked up front (FR-1).
        if BackupStep.DUMP in plan.steps and request.credentials is None:
            require_wp_config_credentials(credentials)
        if handle.opened:
            run.bind_adapters(
                real_adapters(
                    wordpress_source=credentials,
                    secrets=run.secrets,
                    tracer=run.tracer,
                )
            )
        result = backup(
            site,
            request.scope,
            target,
            ctx=run,
            config=config,
            cipher=request.cipher,
            exclusion=request.exclusion,
            origin=None,
        )
        run.run_span.set_attributes(archive_bytes=result.bytes)
        return result


def _cipher(
    *, environ: Mapping[str, str], secrets: SecretRegistry
) -> PayloadCipher | None:
    """The cipher the flags ask for, its passphrase resolved into ``secrets``.

    No flag requests passphrase encryption yet (E002-S02-011), so the AD-26
    chain is not consulted and nothing is prompted: a set variable is noted.
    """
    del secrets
    note_unused_passphrase(environ)
    return None


def run_backup(args: argparse.Namespace, *, secrets: SecretRegistry) -> ExitCode:
    """``wpbackup2 backup``: ``ExitCode.OK``, or raises ``BackupError``.

    ``secrets`` is the root's registry; the Run adopts it, and the root
    reports a failure through it (ADR-0018).
    """
    request = BackupRequest(
        site_root=Path(args.wp_dir),
        scope=backup_scope_from_cli(args.mode),
        target=None if not args.archive else storage_target_from_cli(args.archive),
        wp_config=None if args.wp_config is None else Path(args.wp_config),
        credentials=wordpress_spec(aws_secret_id=None, aws_region=None),
        cipher=_cipher(environ=os.environ, secrets=secrets),
    )
    result = drive_backup(request, ctx=None, secrets=secrets)
    emit_result(str(result.path))
    return ExitCode.OK
