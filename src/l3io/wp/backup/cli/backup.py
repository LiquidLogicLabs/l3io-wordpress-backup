"""AD-1/AD-20 — the backup composition root and the plan executor.

:func:`run_backup` translates the parsed arguments, binds the adapters and
runs :func:`execute_backup_plan`, which performs each step of a
:class:`~l3io.wp.backup.core.backup.BackupPlan` in order, each inside
``phase(step)``. The Archive is written into a staged file beside its final
name and becomes visible only through the store's ``publish()``, after the
verifier passes (AD-20); any failure before that discards the staged file, and
the workspace is removed on every exit (AD-12).

The Secret Registry, the Run Context and the Run ID are E002-S01-004 and
E002-S01-006; exit-code numbers are E002-S01-005. Until then ``run_backup``
returns ``0`` on success and ``1`` on every failure. Encryption binds ``None``
until E008-S01-001 binds Epic 4's cipher; exclusions bind
``INCLUDE_EVERYTHING`` until E002-S01-004 binds the default policy.
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Final, Literal, cast
from urllib.parse import unquote, urlsplit

import structlog

from l3io.wp.backup.adapters.database import (
    LocatedConfig,
    WpDatabase,
    locate_wp_config,
    require_wp_config_credentials,
    site_descriptor_from_wp_config,
)
from l3io.wp.backup.adapters.phase import phase
from l3io.wp.backup.adapters.store import LocalArchiveStore
from l3io.wp.backup.adapters.tar import (
    ArchiveMemberSource,
    SiteRootCollection,
    collect_site_root,
    file_member,
    verify_or_raise,
    write_archive,
)
from l3io.wp.backup.adapters.workspace import run_workspace
from l3io.wp.backup.cli.scope import backup_scope_from_cli
from l3io.wp.backup.core.backup import BackupPlan, BackupStep, build_backup_plan
from l3io.wp.backup.domain.archive_format import (
    CONFIG_RELATIVE,
    DUMP_RELATIVE,
    FORMAT_VERSION,
    PAYLOAD_MEMBER,
    Manifest,
    ManifestExclusions,
    ManifestMember,
    ManifestOrigin,
    ManifestPayload,
    MemberRole,
    source_from_descriptor,
)
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.errors import (
    ArchiveExistsError,
    BackupError,
    InvalidSiteDescriptorError,
    UsageError,
)
from l3io.wp.backup.domain.exclusion import INCLUDE_EVERYTHING
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

__all__ = [
    "BackupResult",
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
    """What a completed backup published."""

    path: PurePosixPath
    scope: BackupScope
    bytes: int
    encrypted: bool


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


def _publish(staged: StagedArchive | None) -> None:
    """``VERIFY`` and ``PUBLISH``, both emitted from inside ``publish()``."""
    if staged is None:
        msg = "VERIFY before WRITE_ARCHIVE"
        raise UsageError(msg)
    staged.publish()


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
                _publish(staged)
            # ENCRYPT runs inside write_archive and PUBLISH inside publish().
    except BaseException:
        if staged is not None:
            staged.discard()
        raise
    if manifest is None:
        msg = "the plan wrote no Archive"
        raise UsageError(msg)
    final = Path(plan.target.path)
    size = final.stat().st_size
    kinds = Counter(member.kind.value for member in manifest.members)
    _log.info(
        "backup_completed",
        path=str(final),
        format_version=manifest.format_version,
        members=dict(sorted(kinds.items())),
        bytes=size,
    )
    return BackupResult(
        path=plan.target.path, scope=plan.scope, bytes=size, encrypted=encrypted
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


def run_backup(args: argparse.Namespace) -> int:
    """``wpbackup2 backup``: ``0`` on success, non-zero on every failure."""
    now = datetime.now(UTC)
    try:
        try:
            site_root = Path(args.wp_dir).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            msg = "site root does not exist"
            raise InvalidSiteDescriptorError(msg, paths=(args.wp_dir,)) from exc
        stated = None if args.wp_config is None else Path(args.wp_config)
        config = locate_wp_config(site_root, stated=stated)
        site, credentials = site_descriptor_from_wp_config(
            site_root, config_file=config.path
        )
        target = storage_target_from_cli(
            args.archive or default_archive_name(site.db_name, now)
        )
        plan = build_backup_plan(
            site, backup_scope_from_cli(args.mode), target, encrypted=False
        )
        # Only a plan that dumps touches the credentials (FR-2, AD-9): a
        # `files` backup neither resolves them nor connects.
        database: DatabaseCommand | None = None
        if BackupStep.DUMP in plan.steps:
            require_wp_config_credentials(credentials)
            database = WpDatabase(credentials)
        store = LocalArchiveStore(verify=verify_or_raise)
        with run_workspace(None) as workspace:
            if store.exists(target):
                raise ArchiveExistsError(target.path)
            result = execute_backup_plan(
                plan,
                site=site,
                config=config,
                workspace=workspace,
                database=database,
                store=store,
                cipher=None,
                exclusion=INCLUDE_EVERYTHING,
                origin=None,
                now=now,
            )
    except BackupError as exc:
        _log.error("backup_failed", error=type(exc).__name__)
        print(f"wpbackup2: error: {exc}", file=sys.stderr)
        return 1
    print(result.path)
    return 0
