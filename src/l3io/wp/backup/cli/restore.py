"""``wpbackup2 restore`` — the restore root (E003-S02-006).

BL-E003-109 split it in three: the values a restore takes and returns, and
the flag translation, are ``cli/restore_request.py``; what a Run prints and
FR-9's exit rule are ``cli/restore_report.py``; this module holds the two
roots below and the driver's steps.

Three layers, each calling only the next, as ``cli/backup.py`` does:

- :func:`run_restore` (the CLI root) translates the flags — through
  :func:`restore_mode_from_cli`, :func:`snapshot_policy_from_cli`,
  :func:`config_policy_from_cli` and ``cli/credentials.admin_spec`` — resolves
  the passphrase into the root's ``SecretRegistry`` (AD-26), builds a frozen
  :class:`RestoreRequest`, makes **one** call to :func:`drive_restore`, prints
  what the returned :class:`RestoreOutcome` carries and applies the exit rule
  (:func:`require_database_restored`). It is the only function here that
  receives ``args``.
- :func:`drive_restore` (the driver Epic 6's library root also calls) opens or
  joins the Run holding the Site Lock, refuses an incomplete Run Journal,
  reads the Site Descriptor (from the target's ``wp-config.php`` or, with
  none, from the Archive's config member after the operator confirms its
  database identity, BL-E002-114), binds the adapter set once, probes the
  Archive, surveys the target through query ports, builds the plan (FR-7's
  guard) and hands it to ``adapters/restore_runner.execute_restore``, which
  runs every phase. It returns or raises; it never prints, except the AD-26
  ``safety snapshot is NOT encrypted`` warning, which is a safety warning,
  not a result.
- The executor, the plan, the direct load with its rollback, and the journal
  are Epic 3's (``core/restore``,
  ``adapters/{restore_runner,database_load,journal}``); this root
  decides none of them. It iterates no phase list: :data:`RestorePhase`
  members are named only as ``... in <x>.phases``.

**The verified bytes (BL-E003-036, RT-001).** Before the Archive is probed it
is copied into the Run's ``0700`` workspace (``0600``, ``O_EXCL``), and every
later read — the probe, ``VERIFY``, the extraction, the config member — reads
that copy. A file swapped at the Storage Target after ``VERIFY`` is never
read. An unencrypted Archive cannot be authenticated (RT-002): the restore
proceeds, logging ``archive_unauthenticated``, and a ``STRUCTURAL_ONLY`` one
(no checksums at all) is also named on stderr; ``docs/restore.md`` states the
trust assumption.

**Dry Run (AD-4).** Selection happens here only: a Run opened for a Dry Run
binds ``dry_run_adapters(real_adapters(...))``; nothing below branches.

``--mode`` is translated through ``cli/scope.py``'s ``CLI_MODE_TO_SCOPE``, the
one place the CLI's ``db`` becomes ``database``; ``render_flags``
(``cli/restore_request.py``) is its inverse for refusals (BL-E003-039).

FR-9's database report (E003-S02-004) and the one ``--log-format json``
object are ``cli/restore_report.py``'s, printed by the root.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import functools
import os
import shutil
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC
from pathlib import Path, PurePath, PurePosixPath
from types import MappingProxyType
from typing import BinaryIO, Final

import structlog

from l3io.wp.backup.adapters.config_writer import refuse_uncarriable
from l3io.wp.backup.adapters.crypto import build_payload_cipher
from l3io.wp.backup.adapters.database import (
    LocatedConfig,
    check_db_tls,
    locate_wp_config,
    password_at_call_time,
    resolve_admin_now,
    site_descriptor_from_archive_config,
    site_descriptor_from_wp_config,
)
from l3io.wp.backup.adapters.database_load import (
    check_live,
    complete_cutover,
    load_live,
)
from l3io.wp.backup.adapters.journal import (
    CURRENT,
    FileRunJournal,
    journal_directory,
)
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.adapters.restore_extension import restore_extension
from l3io.wp.backup.adapters.restore_runner import (
    CONFIG_MAX_BYTES,
    execute_restore,
    snapshot_result,
)
from l3io.wp.backup.adapters.tar import (
    TarArchiveProbe,
    decrypt_payload,
    read_leading_manifest,
)
from l3io.wp.backup.cli import privileges
from l3io.wp.backup.cli.adapter_sets import (
    dry_run_adapters,
    real_adapters,
    real_site_lock,
)
from l3io.wp.backup.cli.backup import (
    NO_ENVIRONMENT,
    backup,
    default_archive_directory,
    storage_target_from_cli,
)
from l3io.wp.backup.cli.context import RunContext, join_or_open
from l3io.wp.backup.cli.credentials import (
    NEW_DB_PASSWORD_FLAG,
    admin_source,
    admin_spec,
    db_tls_setting,
    explicit_secret,
    new_db_password,
    note_password_on_command_line,
    wordpress_source,
    wordpress_spec,
)
from l3io.wp.backup.cli.output import (
    RunWarning,
    WarningSink,
    emit_warning,
)
from l3io.wp.backup.cli.passphrase import (
    note_short_restore_passphrase,
    resolve_passphrase,
    terminal_prompt,
)
from l3io.wp.backup.cli.preflight import require_tools, root_preflight
from l3io.wp.backup.cli.restore_report import (
    print_kept_snapshot,
    print_outcome,
    require_database_restored,
)
from l3io.wp.backup.cli.restore_request import (
    ExistingDatabaseUserError,
    RelocationFlags,
    RestoreExtension,
    RestoreExtensionFactory,
    RestoreOutcome,
    RestoreRequest,
    config_policy_from_cli,
    confirm_identity,
    flag_combination,
    parse_archive_database,
    refusal_for,
    render_flags,
    restore_mode_from_cli,
    snapshot_policy_from_cli,
)
from l3io.wp.backup.cli.settings import setting_origins
from l3io.wp.backup.core.archive_choice import (
    ArchiveSite,
    LatestCandidate,
    choose_latest,
)
from l3io.wp.backup.core.restore import (
    CUTOVER_KEYS,
    WORDPRESS_MARKERS,
    ArchiveHasNoConfigError,
    NonEmptyTargetError,
    RestoreInputs,
    RestorePlan,
    RestoreRelocation,
    SnapshotFailedError,
    SnapshotResult,
    TargetState,
    build_restore_plan,
    database_rollback,
    load_database_name,
    planned_cutover_keys,
    planned_cutover_targets,
)
from l3io.wp.backup.core.restore_journal import (
    incomplete_run_error,
    recovery_command,
    rollback_owed,
)
from l3io.wp.backup.core.restore_space import (
    FREE_SPACE_MARGIN,
    WORKSPACE_FILESYSTEM,
    InsufficientSpaceError,
)
from l3io.wp.backup.domain.archive_format import (
    WP_CONFIG_NAME,
    ArchiveView,
    Integrity,
    Manifest,
    ManifestOrigin,
    OriginKind,
    classify,
    config_member,
    ignore_unknown,
    manifest_from_json,
)
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.database import DatabaseLocation, DatabaseTargetState
from l3io.wp.backup.domain.errors import (
    ArchiveNotEncryptedError,
    ArchiveNotFoundError,
    ArchiveWriteError,
    BackupError,
    ConfigWriteFailedError,
    InvalidSiteDescriptorError,
    MalformedManifestError,
    PassphraseRequiredError,
    RunTerminated,
    SiteRootNotFoundError,
    UnknownInterruptedRunError,
    UsageError,
)
from l3io.wp.backup.domain.exclusion import INCLUDE_EVERYTHING
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.domain.journal import (
    JOURNALED_CONSTANTS,
    JournalHeader,
    JournalRecord,
)
from l3io.wp.backup.domain.restore import (
    ConfigPolicy,
    RestoreMode,
    RestorePhase,
    restores_database,
    restores_files,
)
from l3io.wp.backup.domain.secret import Secret
from l3io.wp.backup.domain.site import SiteDescriptor
from l3io.wp.backup.domain.storage_target import LATEST, StorageTarget
from l3io.wp.backup.ports.config import WHOLE_FILE
from l3io.wp.backup.ports.crypto import PayloadCipher
from l3io.wp.backup.ports.database import DatabaseQuery, DbTls
from l3io.wp.backup.ports.journal import RunJournalCommand, RunJournalQuery
from l3io.wp.backup.ports.lock import SiteLock
from l3io.wp.backup.ports.restore import RestorePorts, restore_ports
from l3io.wp.backup.ports.tracing import Tracer

__all__ = [
    "RESTORE_PROMPT",
    "drive_restore",
    "run_restore",
]

_log = structlog.get_logger(__name__)

#: Written to stderr before any effect when the Archive has no checksums (RT-002).
STRUCTURAL_ONLY_WARNING: Final = (
    "WARNING: the archive carries no checksums (STRUCTURAL_ONLY); restore "
    "only an archive from a source you trust"
)
#: Written to stderr before any effect for every other unencrypted Archive
#: (UX-S02-01; AD-26: a safety warning is never only a log record).
UNAUTHENTICATED_WARNING: Final = (
    "WARNING: the archive is not encrypted, so it cannot be authenticated; "
    "restore only an archive from a source you trust"
)

#: Who acknowledges an interrupted Run resolved from the command line.
_RESOLVED_BY: Final = "cli"
#: The verified copy of the Archive, and the files the root writes, in the workspace.
_ARCHIVE_DIR: Final = "archive"
_DESCRIPTOR_DIR: Final = "descriptor"
_CONFIG_NAME: Final = WP_CONFIG_NAME
_PAYLOAD: Final = "decrypted-payload"
_CONFIG_PAYLOAD: Final = "decrypted-config-payload"
_DIR_MODE: Final = 0o700
_FILE_MODE: Final = 0o600
_STAMP: Final = "%Y%m%dT%H%M%SZ"
#: BL-E003-075: the prompt runs before the Archive is probed (decided
#: 2026-10-27, E003-S02-006), so it says both uses and that empty means none.
RESTORE_PROMPT: Final = (
    "Passphrase (decrypts an encrypted archive and encrypts the Safety "
    "Snapshot; leave empty for none): "
)

# --------------------------------------------------------------------------
# drive_restore and its steps
# --------------------------------------------------------------------------


def _canonical(site_root: Path) -> Path:
    try:
        canonical = Path(site_root).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SiteRootNotFoundError(str(Path(site_root).absolute())) from exc
    if not canonical.is_dir():
        msg = "site root is not a directory"
        raise InvalidSiteDescriptorError(msg, paths=(str(site_root),))
    return canonical


def _private_dir(path: Path) -> Path:
    path.mkdir(mode=_DIR_MODE)
    return path


def _write_private(path: Path, body: bytes) -> Path:
    """``body`` into a new ``0600`` file (``O_EXCL``, ``O_NOFOLLOW``)."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, _FILE_MODE)
        with os.fdopen(fd, "wb") as out:
            os.fchmod(out.fileno(), _FILE_MODE)
            out.write(body)
    except OSError as exc:
        raise ArchiveWriteError(path, exc.strerror or type(exc).__name__) from exc
    return path


def _bind_archive(
    archive: StorageTarget, workspace: Path, opened: BinaryIO | None = None
) -> StorageTarget:
    """RT-001: the Archive copied into the workspace; every later read is of it.

    ``opened`` is the Archive the root opened before it switched to the Site
    Root's owner (owner decision 58), read from its start; ``None`` opens
    ``archive.path`` here.
    """
    source = Path(archive.path)
    destination = _private_dir(workspace / _ARCHIVE_DIR) / (source.name or "archive")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        if opened is None:
            reader = source.open("rb")
        else:
            reader = os.fdopen(os.dup(opened.fileno()), "rb")
            reader.seek(0)
    except OSError as exc:
        raise ArchiveNotFoundError(source) from exc
    try:
        # ADV-S02-02: refuse before the copy what the workspace cannot hold.
        need = os.fstat(reader.fileno()).st_size
        free = shutil.disk_usage(destination.parent).free
        if need > free * (1 - FREE_SPACE_MARGIN):
            reader.close()
            raise InsufficientSpaceError(
                WORKSPACE_FILESYSTEM,
                needed=need,
                free=free,
                paths=(str(destination.parent),),
            )
    except OSError as exc:
        reader.close()
        raise ArchiveWriteError(
            destination, exc.strerror or type(exc).__name__
        ) from exc
    try:
        with reader, os.fdopen(os.open(destination, flags, _FILE_MODE), "wb") as out:
            shutil.copyfileobj(reader, out)
    except OSError as exc:
        destination.unlink(missing_ok=True)
        raise ArchiveWriteError(
            destination, exc.strerror or type(exc).__name__
        ) from exc
    _log.info("archive_bound", archive=str(source), copy=str(destination))
    return dataclasses.replace(archive, path=PurePosixPath(destination))


def _locate(site_root: Path, stated: Path | None) -> LocatedConfig | None:
    """The target's ``wp-config.php``, or ``None`` when it has none."""
    try:
        return locate_wp_config(site_root, stated=stated)
    except InvalidSiteDescriptorError:
        if stated is not None:
            raise
        return None


def _snapshot_dir(request: RestoreRequest, site_root: Path) -> Path:
    """BL-E003-051: absolute and normalised, so the runner's compare holds."""
    chosen = (
        journal_directory(site_root)
        if request.snapshot_dir is None
        else request.snapshot_dir
    )
    return Path(chosen).resolve()


class _Archive:
    """The Archive's verified copy and view, made once and reused (step 5)."""

    def __init__(
        self,
        archive: StorageTarget,
        workspace: Path,
        opened: BinaryIO | None = None,
    ) -> None:
        self._archive = archive
        self._workspace = workspace
        self._opened = opened
        self._copy: StorageTarget | None = None
        self._view: ArchiveView | None = None

    @property
    def copy(self) -> StorageTarget:
        if self._copy is None:
            self._copy = _bind_archive(self._archive, self._workspace, self._opened)
        return self._copy

    def view(self) -> ArchiveView:
        if self._view is None:
            self._view = classify(TarArchiveProbe().member_index(self.copy))
        return self._view


def _require_cipher(view: ArchiveView, cipher: PayloadCipher | None) -> None:
    if view.encrypted and cipher is None:
        operation: Final = "restore"
        raise PassphraseRequiredError(operation)


def _archive_config_bytes(
    archive: _Archive, *, workspace: Path, cipher: PayloadCipher | None
) -> bytes | None:
    """The Archive's config member, read bounded into memory; ``None`` if none."""
    view = archive.view()
    _require_cipher(view, cipher)
    member = config_member(view)
    if member is None:
        return None
    payload: Path | None = None
    if view.encrypted and cipher is not None:
        # Decrypted here so a wrong passphrase stops the Run before anything
        # changes; VERIFY decrypts again into its own file (ADR-0024).
        payload = decrypt_payload(
            archive.copy, view, cipher=cipher, destination=workspace / _CONFIG_PAYLOAD
        )
    try:
        return TarArchiveProbe().read_member(
            archive.copy, view, member.path, max_bytes=CONFIG_MAX_BYTES, payload=payload
        )
    finally:
        if payload is not None:
            payload.unlink(missing_ok=True)


def _archive_config(
    archive: _Archive, *, workspace: Path, cipher: PayloadCipher | None
) -> Path:
    """The Archive's config member, read bounded into a ``0600`` workspace file."""
    body = _archive_config_bytes(archive, workspace=workspace, cipher=cipher)
    if body is None:
        msg = "neither the target nor the archive has a wp-config.php"
        raise InvalidSiteDescriptorError(msg, paths=(str(archive.copy.path),))
    directory = _private_dir(workspace / _DESCRIPTOR_DIR)
    return _write_private(directory / _CONFIG_NAME, body)


def _archive_config_text(
    archive: _Archive, *, workspace: Path, cipher: PayloadCipher | None, target: Path
) -> str:
    """The Archive's config text, in memory only, as ``REWRITE_CONFIG`` reads it.

    No copy is written (BL-E003-057). An Archive without one is
    ``ArchiveHasNoConfigError``, as the plan refuses it; text that is not
    UTF-8 is ``ConfigWriteFailedError``, as the executor refuses it.
    """
    body = _archive_config_bytes(archive, workspace=workspace, cipher=cipher)
    if body is None:
        raise ArchiveHasNoConfigError(str(archive.copy.path))
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError as exc:
        reason = "the replacement could not be read: UnicodeDecodeError"
        raise ConfigWriteFailedError(target, WHOLE_FILE, reason) from exc


#: The relocation flag that rewrites each ``DB_*`` key at the cutover.
_RELOCATED_KEYS: Final = (
    ("new_db_host", "DB_HOST"),
    ("new_db_user", "DB_USER"),
    ("new_db_password", "DB_PASSWORD"),
    ("new_db_name", "DB_NAME"),
)


def _refused_on_environment(
    request: RestoreRequest,
) -> tuple[str | None, tuple[str, ...]]:
    """What this restore would rewrite that an environment-driven ``DB_*`` refuses.

    Owner decisions 47 and 49, ADR-0027: a database restore loads straight
    into the database the environment names and writes no ``DB_*``, so only
    a relocation's cutover can be refused: the keys its ``--new-db-*``
    flags rewrite. ``(None, ())`` otherwise; the lookups are then resolved,
    and ``--overwrite-wp-config`` carries them into the Archive's file as
    source (owner decision 48, :func:`_refuse_uncarriable`).
    """
    flags = request.relocation
    keys = tuple(
        key for name, key in _RELOCATED_KEYS if getattr(flags, name) is not None
    )
    if not keys or not restores_database(request.mode):
        return None, ()
    return "a relocation of the database (--new-db-*)", keys


def _refuse_uncarriable(
    archive: _Archive, workspace: Path, request: RestoreRequest, config_file: Path
) -> None:
    """``--overwrite-wp-config``: refuse before any effect what cannot be carried.

    Owner decision 48: the target's ``DB_*`` are carried into the Archive's
    file as source. A ``getenv_docker()`` among them needs the Archive's file
    to define that function; ``refuse_uncarriable`` reads the Archive's
    config (into memory, only then) and refuses here, a Dry Run included,
    rather than at ``REWRITE_CONFIG`` after ``CLEAN``.
    """
    refuse_uncarriable(
        config_file,
        CUTOVER_KEYS,
        archive_content=lambda: _archive_config_text(
            archive, workspace=workspace, cipher=request.cipher, target=config_file
        ),
    )


def _db_user(run: RunContext, config_file: Path) -> str:
    values = run.adapters.config_query.read_constants(config_file, ("DB_USER",))
    value = values.get("DB_USER")
    return "" if value is None else str(value)


def _incomplete(
    query: RunJournalQuery, site_root: Path, resolve_run_id: str | None
) -> JournalRecord | None:
    """Step 2: the blocking record to resolve, or ``None``; else refuse."""
    record = query.incomplete(PurePosixPath(site_root))
    if record is None:
        if resolve_run_id is not None:
            raise UnknownInterruptedRunError(resolve_run_id, recorded_run_id=None)
        return None
    if resolve_run_id == record.header.run_id:
        return record
    error = incomplete_run_error(record, str(journal_directory(site_root) / CURRENT))
    _log.error(
        "incomplete_run_found",
        run_id_interrupted=error.run_id,
        started_at=error.started_at,
        interrupted_phase=None
        if error.interrupted_phase is None
        else error.interrupted_phase.value,
        undo=error.undo,
        journal_path=error.journal_path,
    )
    raise error


def _ports(
    run: RunContext,
    site: SiteDescriptor,
    cutover: Callable[[], str] | None,
) -> RestorePorts:
    """The one call of ``restore_ports``; preflight exactly as ``cli/backup.py``."""
    return restore_ports(
        run.adapters,
        preflight=functools.partial(
            require_tools,
            root_preflight(run, site, credential_source=site.credential_source),
        ),
        free_bytes=lambda path: shutil.disk_usage(path).free,
        cutover_credentials=cutover,
    )


def _surveyor(
    run: RunContext,
    *,
    site: SiteDescriptor,
    site_root: Path,
    target_config: Path,
    mode: RestoreMode,
    relocation: RestoreRelocation,
    wordpress_user: str,
) -> Callable[[], TargetState]:
    """The target's survey, through query ports only (BL-E003-035).

    In every Mode that restores the database the live database is surveyed,
    as the WordPress account or, where it cannot see the database, the
    administrator (owner decision 55, AD-29 as amended), so
    ``TargetState.database`` is ``None`` only for a Mode that does not touch
    it: "not looked at" is never read as "empty". A relocated load database
    is surveyed as the account that loads it.
    """
    location = site.database_location()

    def database(
        at: DatabaseLocation | None, query: DatabaseQuery
    ) -> DatabaseTargetState | None:
        if at is None or not restores_database(mode):
            return None
        return query.survey_target(
            at, table_prefix=site.table_prefix, wordpress_user=wordpress_user or None
        )

    def survey() -> TargetState:
        files = run.adapters.site_files_query
        live = database(location, run.adapters.database_query)
        if restores_database(mode) and live is None:  # pragma: no cover - by type
            msg = "a database Mode was planned without surveying the live database"
            raise ValueError(msg)
        return TargetState(
            site_root_wordpress=any(
                files.file_exists(site_root / name) for name in WORDPRESS_MARKERS
            ),
            site_root_empty=files.is_empty(site_root),
            database=live,
            config_exists=files.file_exists(
                Path(relocation.config_file or target_config)
            ),
            site_root=PurePosixPath(site_root),
            site_root_entries=files.entry_count(site_root),
            database_location=location if live is not None else None,
            load_target=database(
                relocation.load_location, run.adapters.restore_database_query
            ),
            load_location=relocation.load_location,
        )

    return survey


def _log_plan(plan: RestorePlan, target: TargetState) -> None:
    _log.info(
        "target_checked",
        site_root_empty=target.site_root_empty,
        database_exists=None
        if target.database is None
        else target.database.database_exists,
        table_count=None if target.database is None else target.database.table_count,
        overrides=sorted(o.value for o in plan.overrides.waived),
    )
    for action in plan.actions:
        _log.info(
            "planned_action",
            ordinal=action.ordinal,
            phase=action.phase.name,
            action=action.text,
            fields=dict(action.fields),
        )


def _prior_constants(run: RunContext, config_file: Path) -> dict[str, str | None]:
    keys = sorted(JOURNALED_CONSTANTS)
    read = run.adapters.config_query.read_constants(config_file, keys)
    return {k: None if read.get(k) is None else str(read.get(k)) for k in keys}


def _require_reset_consent(
    request: RestoreRequest,
    target: TargetState,
    *,
    config_from_archive: bool,
    user: str,
    location: DatabaseLocation,
) -> None:
    """RT-S02-004: refuse resetting an existing account's password from the
    Archive without ``--reset-existing-db-user``; existence is the
    administrator's survey (AD-29): ``user_exists`` is known only when the
    administrator surveyed, which is when the account could not log in, so
    an administrator's ``ensure`` would set its password (owner decision
    55)."""
    if (
        config_from_archive
        and restores_database(request.mode)
        and target.database is not None
        and target.database.user_exists is True
        and not request.reset_existing_db_user
    ):
        raise ExistingDatabaseUserError(user=user, location=location)


def drive_restore(
    request: RestoreRequest,
    *,
    ctx: RunContext | None,
    tracer: Tracer | None = None,
    secrets: SecretRegistry | None = None,
    command_args: Sequence[str] | None = None,
    site_lock: SiteLock | None = None,
    on_warning: WarningSink | None = None,
    environ: Mapping[str, str] = NO_ENVIRONMENT,
    extension: RestoreExtensionFactory = restore_extension,
) -> RestoreOutcome:
    """The restore driver: returns a :class:`RestoreOutcome` or raises; never prints.

    ``extension`` builds the relocation half (Epic 7's
    ``adapters/restore_extension.py`` factory, the one this build ships); a
    test passes its own. It is a parameter, not a module global (BL-E003-099),
    so nothing is patched.

    ``environ`` is what the credential builders read (the administrator's
    and the relocated account's variables): the CLI root and the library pass
    ``os.environ``; no driver reads it itself (owner decision 2026-11-07,
    BL-E006-013).

    Its operator notices (``RunWarning``) go to ``on_warning`` when given,
    else to stderr (``cli/output.emit_warning``); a notice's WARNING record is
    logged either way.

    Steps, each before the next, nothing destructive before the executor:
    the Site Lock and the Run (1); the journal query (2); the Site Descriptor,
    the credential sources and the one bind (3), with the Archive identity
    confirmed on the no-config path before any database call; resolution of
    an interrupted Run, which stops the Run (4); the probe (5); the plan,
    FR-7's guard included (6); the header and inputs (7); ``execute_restore``
    (8); the outcome (9). An error escaping after ``SNAPSHOT`` completed
    carries ``snapshot_path``.
    """
    # Owner decision 58: the CLI root has switched to the Site Root's owner
    # by now; a library caller running as root is refused.
    privileges.require_not_root(request.site_root, operation="restore")
    if request.config_policy is ConfigPolicy.OVERWRITE and not restores_files(
        request.mode
    ):
        msg = (
            "--overwrite-wp-config needs a mode that restores files; "
            f"Restore Mode {request.mode.value} restores none"
        )
        raise UsageError(msg, arguments=("config_policy",))
    if request.archive_database is not None:
        parse_archive_database(request.archive_database)
    site_root = _canonical(request.site_root)
    with join_or_open(
        ctx,
        operation="restore",
        tracer=tracer,
        secrets=secrets,
        command_args=command_args,
        dry_run=request.dry_run,
        site_root=site_root,
        site_lock=real_site_lock() if site_lock is None else site_lock,
    ) as handle:
        run = handle.ctx
        _log.info(
            "restore_mode_resolved",
            restore_mode=request.mode.value,
            overrides=sorted(o.value for o in request.overrides.waived),
            snapshot_policy=request.snapshot.value,
            flags=request.mode_flags,
        )
        # Step 2: the journal, keyed by the canonical Site Root only.
        pair: tuple[RunJournalQuery, RunJournalCommand] | None = None
        if handle.opened:
            journal = FileRunJournal(journal_directory(site_root))
            pair = (journal, journal)
            query: RunJournalQuery = journal
        else:
            query = run.adapters.journal_query
        resolving = _incomplete(query, site_root, request.resolve_run_id)
        # The factory registers --new-db-password with the Run's registry
        # before anything reads the Site or the Archive (BL-E003-094: once).
        ext: RestoreExtension = extension(request.relocation, secrets=run.secrets)
        # Step 3: the Site Descriptor, the credential sources, the one bind.
        archive = _Archive(request.archive, run.workspace, request.archive_handle)
        notes: list[str] = list(request.notes)
        located = _locate(site_root, request.relocation.wp_config)
        config_from_archive = located is None
        refused_operation, refused_keys = _refused_on_environment(request)
        if located is not None:
            site, wp_config = site_descriptor_from_wp_config(
                site_root,
                config_file=located.path,
                environ=environ,
                secrets=run.secrets,
                refuse_environment=refused_operation,
                refuse_keys=refused_keys,
            )
            target_config, config_dir = located.path, located.path.parent
            descriptor_file = located.path
            if request.config_policy is ConfigPolicy.OVERWRITE and resolving is None:
                _refuse_uncarriable(archive, run.workspace, request, located.path)
        else:
            descriptor_file = _archive_config(
                archive, workspace=run.workspace, cipher=request.cipher
            )
            site, wp_config = site_descriptor_from_archive_config(
                site_root,
                config_file=descriptor_file,
                environ=environ,
                secrets=run.secrets,
                refuse_environment=refused_operation,
                refuse_keys=refused_keys,
            )
            located = LocatedConfig(path=descriptor_file, location=None)
            target_config, config_dir = site_root / _CONFIG_NAME, site_root
        run.run_span.set_attributes(db_name=site.db_name)
        # BL-E002-114: an override below the host's TLS policy is USAGE here,
        # before any connection.
        check_db_tls(request.db_tls)
        admin = admin_source(request.admin, environ=environ, secrets=run.secrets)
        # The restored account is wp-config.php's alone: the survey, the
        # reset-consent check and the restored config all name its DB_USER, so
        # L3IO_WP_BACKUP_DB_* (a backup's override) must not pick another
        # account to provision (E005-S01-001 review F-1).
        # ``--db-credentials-aws-*`` (E008-S01-001) is that account's stated
        # source; the environment still is not consulted.
        wordpress = wordpress_source(
            request.credentials, wp_config=wp_config, environ={}, secrets=run.secrets
        )
        relocated_spec = ext.wordpress_spec()
        relocated = (
            None
            if relocated_spec is None
            else wordpress_source(
                relocated_spec,
                wp_config=wp_config,
                environ=environ,
                secrets=run.secrets,
            )
        )
        if handle.opened:
            # AD-4: the one place a Dry Run is selected — by adapter set.
            real = real_adapters(
                wordpress_source=wordpress,
                secrets=run.secrets,
                tracer=run.tracer,
                admin_source=admin,
                restore_wordpress_source=relocated,
                search_replace=ext.search_replace(),
                journal=pair,
                db_tls=request.db_tls,
                create_archive_directory=True,
                environ=environ,
            )
            run.bind_adapters(dry_run_adapters(real) if request.dry_run else real)
        db_user = _db_user(run, descriptor_file)
        if located.location is None and descriptor_file != target_config:
            if archive.view().encrypted:
                # Owner decision 65: the passphrase authenticates the
                # Archive, so its config's database is used as it says.
                notes.append(_trusted_database(site, db_user))
            else:
                confirm_identity(request.archive_database, site=site, user=db_user)
        elif request.archive_database is not None:
            _log.info("archive_database_unused", reason="the target has wp-config.php")
        cutover = None if relocated is None else password_at_call_time(relocated)
        ports = _ports(run, site, cutover)
        # Step 4: resolution only — resolving and restoring are never one command.
        if resolving is not None:
            record = resolving
            complete_cutover(
                record,
                ports=ports,
                table_prefix=site.table_prefix,
                journal_path=str(journal_directory(site_root) / CURRENT),
            )
            _note_rollback_owed(record)
            run.adapters.journal_command.resolve(
                record.header.run_id, resolved_by=_RESOLVED_BY
            )
            _log.info("interrupted_run_resolved", run_id_resolved=record.header.run_id)
            return RestoreOutcome(
                run_id=run.run_id,
                mode=request.mode,
                phases=(),
                dry_run=run.dry_run,
                actions=(),
                integrity=None,
                snapshot_path=None,
                snapshot_encrypted=False,
                database=None,
                search_replace=None,
                resolved_run_id=record.header.run_id,
                skipped_links=(),
                wp_config_kept=False,
                notes=tuple(notes),
            )
        # Step 5: the probe (reused when step 3 already probed).
        view = archive.view()
        _note_allow_unencrypted(request)
        if not view.encrypted and request.require_encrypted:
            # RT-1: the operator said an encrypted Archive was expected; a
            # plain one may be a substitution. Refused before any effect.
            raise ArchiveNotEncryptedError(str(request.archive.path), required=True)
        if not view.encrypted and request.cipher is not None:
            # Owner decision 59 (ADR-0016 amendment reversed): a passphrase
            # was given, so an encrypted Archive was expected; warned, before
            # any effect, and restored. --require-encrypted is the hard stop.
            _warn_not_encrypted(str(request.archive.path), on_warning)
        if not view.encrypted:
            # RT-002 waiver (ADR-0016 amendment, 2026-11-06): restore, warning
            # before any effect, so the warning is seen even if the Run fails.
            _log.warning(
                "archive_unauthenticated",
                integrity=view.integrity.value,
                archive=str(request.archive.path),
            )
            emit_warning(
                RunWarning(
                    code="archive_unauthenticated",
                    message=STRUCTURAL_ONLY_WARNING
                    if view.integrity is Integrity.STRUCTURAL_ONLY
                    else UNAUTHENTICATED_WARNING,
                    fields=MappingProxyType(
                        {
                            "integrity": view.integrity.value,
                            "archive": str(request.archive.path),
                        }
                    ),
                ),
                on_warning,
            )
        # Step 6: the plan, FR-7's guard included.
        _require_cipher(view, request.cipher)
        relocation = ext.resolve(
            site_root=site_root,
            site=site,
            view=view,
            files=run.adapters.site_files_query,
            mode=request.mode,
        )
        survey = _surveyor(
            run,
            site=site,
            site_root=site_root,
            target_config=target_config,
            mode=request.mode,
            relocation=relocation,
            wordpress_user=relocation.cutover_user or db_user,
        )
        snapshot_dir = _snapshot_dir(request, site_root)
        try:
            target = survey()
            _require_reset_consent(
                request,
                target,
                config_from_archive=config_from_archive and relocated_spec is None,
                user=db_user,
                location=site.database_location(),
            )
            plan = build_restore_plan(
                site,
                request.mode,
                request.overrides,
                view,
                target,
                archive=request.archive,
                relocation=relocation,
                snapshot=request.snapshot,
                snapshot_dir=snapshot_dir,
                config_dir=config_dir,
                config_policy=request.config_policy,
                extra_tools=request.extra_tools + ext.required_tools(),
            )
        except NonEmptyTargetError as exc:
            raise refusal_for(exc) from exc
        _log_plan(plan, target)
        # Step 7: the journal header and the executor's inputs.
        stamp = run.started_at.astimezone(UTC).strftime(_STAMP)
        snapshot_path = snapshot_dir / f"{stamp}-{run.run_id}.tar.gz"
        saved_config = (
            str(snapshot_dir / f"{stamp}-{run.run_id}-wp-config.php")
            if request.config_policy is ConfigPolicy.OVERWRITE
            else None
        )
        load = load_database_name(site.db_name, relocation)
        header = JournalHeader(
            run_id=run.run_id,
            started_at=run.started_at.astimezone(UTC).isoformat(),
            site_root=PurePosixPath(site_root),
            restore_mode=request.mode,
            archive=str(request.archive.path),
            # ADV-S02-01: a path only when the plan takes the snapshot, so the
            # undo text never points at a file that was never written.
            snapshot_path=(
                str(snapshot_path) if RestorePhase.SNAPSHOT in plan.phases else None
            ),
            live_database_before=site.db_name,
            # Owner decision 49: there is no staging database any more; the
            # field stays so an earlier version's journal reads back.
            staging_database=None,
            wp_config_path=str(relocation.config_file or target_config),
            load_database=load,
            cutover_keys=planned_cutover_keys(relocation),
            cutover_targets=planned_cutover_targets(load, relocation),
            prior_constants=_prior_constants(run, descriptor_file),
            phase_undo=dict(relocation.undo),
            saved_config=saved_config,
        )
        inputs = RestoreInputs(
            site=site,
            view=view,
            archive=archive.copy,
            run_id=run.run_id,
            relocation=relocation,
            header=header,
            config_policy=request.config_policy,
            config_path=target_config,
            payload=run.workspace / _PAYLOAD if view.encrypted else None,
            workspace=run.workspace,
            live_database_exists=target.database is not None
            and target.database.database_exists,
            archive_name=str(request.archive.path),
            rollback=database_rollback(site, relocation, target, plan),
            prefixed_tables=_prefixed_tables(relocation, target),
        )
        taken: list[SnapshotResult] = []

        def take_snapshot(scope: BackupScope) -> SnapshotResult:
            result = backup(
                site,
                scope,
                storage_target_from_cli(str(snapshot_path)),
                ctx=run,
                config=located,
                cipher=request.cipher,
                exclusion=INCLUDE_EVERYTHING,
                origin=ManifestOrigin(
                    kind=OriginKind.SAFETY_SNAPSHOT,
                    restore_mode=request.mode.value,
                    run_id=run.run_id,
                ),
            )
            taken.append(
                snapshot_result(
                    PurePosixPath(result.path),
                    scope=result.scope,
                    encrypted=result.encrypted,
                    size=result.bytes,
                    performed=result.performed,
                )
            )
            if not request.dry_run and not taken[-1].performed:
                # The executor accepts a not-performed snapshot from its result
                # alone (ADR-0022 option D); a real Run never may.
                reason = "the snapshot was not performed, and the restore would destroy"
                raise SnapshotFailedError(str(snapshot_path), reason)
            if request.cipher is None and taken[-1].performed:
                _warn_after_snapshot(
                    RunWarning(
                        code="safety_snapshot_unencrypted",
                        message=f"safety snapshot is NOT encrypted: {result.path}",
                        fields=MappingProxyType({"path": str(result.path)}),
                    ),
                    on_warning,
                )
                _log.warning("safety_snapshot_unencrypted", path=str(result.path))
            elif taken[-1].performed:
                # BL-E003-075: restoring it needs this Run's passphrase.
                _warn_after_snapshot(
                    RunWarning(
                        code="safety_snapshot_encrypted",
                        message="safety snapshot is encrypted with the passphrase "
                        f"this restore was given: {result.path}",
                        fields=MappingProxyType({"path": str(result.path)}),
                    ),
                    on_warning,
                )
                # WARNING, as the unencrypted case: under --log-format json
                # this record is the only place the notice reaches stderr, so
                # it must pass the default and a WARNING level.
                _log.warning("safety_snapshot_encrypted", path=str(result.path))
            return taken[-1]

        # Step 8: the executor runs every phase.
        try:
            report = execute_restore(
                plan,
                inputs,
                ports,
                take_snapshot,
                functools.partial(check_live, ports=ports),
                functools.partial(
                    load_live,
                    ports=ports,
                    workspace=run.workspace,
                    cipher=request.cipher,
                    clock=run.clock,
                ),
                run.adapters.journal_command,
                request.cipher,
                survey_target=survey,
                opted_out_by=request.opted_out_by,
            )
        except NonEmptyTargetError as exc:
            refusal = refusal_for(exc)
            _carry_snapshot(refusal, taken)
            raise refusal from exc
        except BaseException as exc:
            # ADV-S02-06: whatever escapes — a defect, an OSError, Ctrl-C —
            # still names the copy of the Site this Run took.
            _carry_snapshot(exc, taken)
            raise
        # Step 9: the outcome.
        snapshot = report.snapshot
        return RestoreOutcome(
            run_id=run.run_id,
            mode=request.mode,
            phases=report.phases,
            dry_run=run.dry_run,
            actions=plan.actions,
            integrity=report.integrity,
            snapshot_path=None if snapshot is None else snapshot.path,
            snapshot_encrypted=snapshot is not None and snapshot.encrypted,
            database=report.database,
            search_replace=report.search_replace,
            resolved_run_id=None,
            # Owner decision 41: a Dry Run extracts nothing, so it reports the
            # links the plan says the real Run would skip.
            skipped_links=plan.skipped_links if run.dry_run else report.skipped_links,
            wp_config_kept=report.wp_config_kept,
            notes=tuple(notes),
        )


def _prefixed_tables(
    relocation: RestoreRelocation, target: TargetState
) -> tuple[str, ...]:
    """The load database's tables with the site's prefix, as surveyed (decision 53).

    What a Dry Run's database report says would be dropped; ``()`` when the
    survey could not list them (the plan's line then names the prefix).
    """
    surveyed = (
        target.load_target
        if relocation.load_location is not None and target.load_target is not None
        else target.database
    )
    if surveyed is None or surveyed.prefixed_tables is None:
        return ()
    return surveyed.prefixed_tables


#: The attribute an escaping exception carries the Safety Snapshot's path on:
#: ``BackupError.snapshot_path``, set the same way on any other exception.
_SNAPSHOT_ATTR: Final = "snapshot_path"


def _carry_snapshot(exc: BaseException, taken: list[SnapshotResult]) -> None:
    """AG-15, ADV-S02-06: an error after a performed snapshot names it.

    A ``BackupError`` carries the path in ``snapshot_path``; any other
    exception gets the same attribute, as ``cli/context.py`` leaves the Run
    ID on one. An exception that cannot take an attribute keeps only the
    ``safety_snapshot_kept`` record.
    """
    if not (taken and taken[-1].performed):
        return
    path = taken[-1].path
    _log.info("safety_snapshot_kept", path=str(path))
    with contextlib.suppress(AttributeError, TypeError):
        setattr(exc, _SNAPSHOT_ATTR, path)


def _kept_snapshot(exc: BaseException) -> PurePath | None:
    """The Safety Snapshot path :func:`_carry_snapshot` left on ``exc``."""
    path = getattr(exc, _SNAPSHOT_ATTR, None)
    return path if isinstance(path, PurePath) else None


def _warn_after_snapshot(warning: RunWarning, sink: WarningSink | None) -> None:
    """``emit_warning``, where a raising ``sink`` must not abort the Run.

    BL-E008-022 (owner decision 2026-11-07): once the Safety Snapshot is
    taken, a library caller's ``on_warning`` that raises (a closed log handle,
    a UI callback) would stop the restore with a snapshot and a journal entry
    but no restore. Its exception is logged (ERROR ``on_warning_failed``, the
    notice's code and the exception's class) and the Run continues; the
    notice's own WARNING record is logged by the caller either way. A notice
    before any effect still propagates the sink's exception.
    """
    try:
        emit_warning(warning, sink)
    except RunTerminated:
        raise
    except Exception as exc:  # RunTerminated: mapped here (re-raised above)
        _log.error("on_warning_failed", code=warning.code, error=type(exc).__name__)


def _note_rollback_owed(record: JournalRecord) -> None:
    """WARNING ``database_rollback_owed`` when the resolved Run left the live
    database partly loaded (owner decision 50).

    Resolving acknowledges; it never restores (resolving and restoring are
    never one command), so the record names the Safety Snapshot and the
    command that restores it.
    """
    if not rollback_owed(record):
        return
    header = record.header
    _log.warning(
        "database_rollback_owed",
        run_id_resolved=header.run_id,
        database=header.load_database,
        snapshot=header.snapshot_path,
        recovery=recovery_command(header) or None,
    )


def _note_allow_unencrypted(request: RestoreRequest) -> None:
    """WARNING ``deprecated_option`` when ``allow_unencrypted`` is given.

    Owner decision 59: a plain Archive restored with a passphrase proceeds
    with a warning, so the option has no effect left; it is accepted, so a
    script using it keeps running, and said to be deprecated.
    """
    if request.allow_unencrypted:
        _log.warning(
            "deprecated_option",
            option="allow_unencrypted",
            replacement=None,
            reason="has no effect: a plain archive restored with a passphrase "
            "proceeds with a warning; require_encrypted refuses it",
        )


def _warn_not_encrypted(archive: str, on_warning: WarningSink | None) -> None:
    """Owner decision 59: WARNING ``archive_not_encrypted``, and a notice."""
    reason = (
        "a passphrase was given but the archive is not encrypted; it may have "
        "been substituted for an encrypted one (--require-encrypted refuses it)"
    )
    _log.warning("archive_not_encrypted", archive=archive, reason=reason)
    emit_warning(
        RunWarning(
            code="archive_not_encrypted",
            message=f"WARNING: {reason}: {archive}",
            fields=MappingProxyType({"archive": archive}),
        ),
        on_warning,
    )


def _trusted_database(site: SiteDescriptor, user: str) -> str:
    """Owner decision 65: an encrypted Archive's database, logged; its note."""
    location = site.database_location()
    _log.info(
        "archive_database_trusted",
        db_host=location.host,
        db_port=location.port,
        db_name=location.name,
        db_user=user,
        reason="the archive is encrypted, so its passphrase authenticates it",
    )
    return (
        f"database from the archive's wp-config.php (encrypted, so trusted): "
        f"{user}@{location.host}:{location.port}/{location.name}"
    )


#: The origins whose secret the chain reads for itself (owner decision 60).
_CHAIN_ORIGINS: Final = frozenset({"environment", "default", None})
#: The Archive file names ``--archive latest`` reads.
_ARCHIVE_GLOB: Final = "*.tar.gz"


def _admin_pair(
    user: str | None, password: Secret | str | None, origins: Mapping[str, str]
) -> tuple[str | None, Secret | str | None]:
    """The administrator flags as ``admin_spec`` takes them.

    Both from the environment (or unset): ``(None, None)``, the chain's own
    environment step. Either typed or from the config file: both values, and
    ``admin_spec`` takes a missing password from the variable, as before.
    """
    if all(origins.get(n) in _CHAIN_ORIGINS for n in ("admin_user", "admin_password")):
        return None, None
    return user, password


def _db_tls(
    db_tls: str | None,
    db_tls_ca: str | None,
    *,
    admin_tls: str | None,
    admin_tls_ca: str | None,
) -> DbTls:
    """``--db-tls``/``--db-tls-ca``, or their deprecated ``--admin-tls*`` spellings.

    Owner decision 56: each deprecated spelling logs WARNING
    ``deprecated_option`` and is taken as the new one; both, disagreeing,
    are ``UsageError``.
    """
    pairs = (
        ("--admin-tls", admin_tls, "--db-tls", db_tls),
        ("--admin-tls-ca", admin_tls_ca, "--db-tls-ca", db_tls_ca),
    )
    for old, old_value, new, new_value in pairs:
        if old_value is None:
            continue
        _log.warning("deprecated_option", option=old, replacement=new)
        if new_value is not None and new_value != old_value:
            msg = f"{old} and {new} disagree; give only {new}"
            raise UsageError(msg, arguments=(new,))
    return db_tls_setting(db_tls or admin_tls, db_tls_ca or admin_tls_ca)


def _manifest(path: Path) -> Manifest | None:
    text = read_leading_manifest(path)
    if text is None:
        return None
    try:
        return manifest_from_json(text, on_unknown=ignore_unknown)
    except MalformedManifestError:
        return None


def _latest(
    site_root: Path,
    *,
    archive_dir: str | None,
    wp_config: str | None,
    secrets: SecretRegistry,
) -> tuple[StorageTarget, str]:
    """Owner decision 64: the Archive ``--archive latest`` names, and its note.

    "This Site" is the target's ``wp-config.php``'s database and the canonical
    Site Root (``core/archive_choice.py``), so a target without one is
    refused: nothing would tell its Archives from another site's.
    """
    canonical = _canonical(site_root)
    located = _locate(canonical, None if wp_config is None else Path(wp_config))
    if located is None:
        msg = (
            "--archive latest needs the target's wp-config.php to tell this "
            "site's archives from another site's; name the archive instead"
        )
        raise UsageError(msg, arguments=("archive",))
    site, _ = site_descriptor_from_wp_config(
        canonical, config_file=located.path, environ=os.environ, secrets=secrets
    )
    directory = (
        Path(archive_dir).resolve()
        if archive_dir is not None
        else default_archive_directory(canonical)[0]
    )
    paths = sorted(p for p in directory.glob(_ARCHIVE_GLOB) if p.is_file())
    candidates = [
        LatestCandidate(path=PurePosixPath(p), manifest=_manifest(p)) for p in paths
    ]
    choice = choose_latest(
        candidates,
        ArchiveSite.from_descriptor(site),
        directory=PurePosixPath(directory),
    )
    _log.info(
        "archive_latest_selected",
        archive=str(choice.path),
        created_at=choice.created_at,
        archive_dir=str(directory),
        considered=choice.considered,
        matched=choice.matched,
    )
    note = (
        f"archive latest: {choice.path} (created {choice.created_at}; the newest "
        f"of {choice.matched} archives of this site in {directory})"
    )
    return storage_target_from_cli(str(choice.path)), note


def _note_drop_database(given: bool) -> None:
    """Owner decision 63: ``--drop-database`` is accepted and does nothing more.

    Every database restore loads straight into the live database and drops
    the tables carrying the site's prefix first (decisions 49 and 53), so
    there is no previous database left to remove. A script that passes the
    flag keeps running; WARNING ``deprecated_option`` says it has no effect.
    """
    if given:
        _log.warning(
            "deprecated_option",
            option="--drop-database",
            replacement=None,
            reason="has no effect: a database restore loads into the live "
            "database and leaves no previous database to remove",
        )


def _admin_as_root(
    request: RestoreRequest, *, environ: Mapping[str, str], secrets: SecretRegistry
) -> RestoreRequest:
    """Owner decision 71: the administrator's chain, resolved while still root.

    Only when one is configured (the flags, an AWS secret, or the
    ``L3IO_WP_BACKUP_ADMIN_*`` pair): the answer is carried into the Run as a
    ``ResolvedCredentialSpec`` and nothing is read again after the switch.
    It is still used only where an administrator is needed (decision 55).
    """
    source = admin_source(request.admin, environ=environ, secrets=secrets)
    if source is None:
        return request
    return dataclasses.replace(request, admin=resolve_admin_now(source))


def _open_archive(archive: StorageTarget) -> BinaryIO:
    """The Archive, opened before the root switches users (owner decision 58)."""
    path = Path(archive.path)
    try:
        return path.open("rb")
    except OSError as exc:
        raise ArchiveNotFoundError(path) from exc


def run_restore(args: argparse.Namespace, *, secrets: SecretRegistry) -> ExitCode:
    """``wpbackup2 restore``: ``ExitCode.OK``, or raises ``BackupError``.

    Argument translation only, then one :func:`drive_restore` call. The
    passphrase is resolved into ``secrets`` on every restore (AD-26) so the
    one passphrase decrypts an encrypted Archive and encrypts the Safety
    Snapshot; with none, the snapshot is written unencrypted and said so.

    Started as root (owner decision 58), everything only root may read is
    read first — the passphrase, ``--new-db-password-file``, the target's
    ``wp-config.php`` for ``--archive latest``, and the Archive, kept open —
    and then the process switches to the Site Root's owner
    (``cli/privileges.drop_to_site_owner``) before the Run opens.
    """
    origins = setting_origins(vars(args))
    admin_user, admin_password = _admin_pair(
        args.admin_user, args.admin_password, origins
    )
    typed = origins.get("admin_password", "command line")
    if args.admin_password is not None and typed == "command line":
        # RT-03: the notice ``--new-db-password`` gives, never the value.
        note_password_on_command_line("--admin-password")
    relocated_origin = origins.get("new_db_password")
    # Passthrough: copied verbatim, validated by Epic 7.
    relocation = RelocationFlags(
        new_site_url=args.new_site_url,
        new_home_url=args.new_site_home_url,
        new_db_host=args.new_db_host,
        new_db_name=args.new_db_name,
        new_db_user=args.new_db_user,
        new_db_password=new_db_password(
            explicit=None
            if relocated_origin == "environment"
            else args.new_db_password,
            file=args.new_db_password_file,
            environ=os.environ,
            secrets=secrets,
            user_given=args.new_db_user is not None,
            explicit_source="config file key new_db_password"
            if relocated_origin == "config file"
            else NEW_DB_PASSWORD_FLAG,
        ),
        wp_config=None if args.wp_config is None else Path(args.wp_config),
    )
    mode, overrides = restore_mode_from_cli(
        mode=args.mode,
        cleanfirst=args.cleanfirst,
        drop_database=args.drop_database,
        force=args.force,
    )
    _note_drop_database(bool(args.drop_database))
    passphrase, passphrase_source = explicit_secret(
        args.passphrase, origins.get("passphrase"), name="passphrase"
    )
    resolved = resolve_passphrase(
        explicit=passphrase,
        environ=os.environ,
        prompt=terminal_prompt(),
        secrets=secrets,
        label=RESTORE_PROMPT,
        explicit_source=passphrase_source,
    )
    note_short_restore_passphrase(resolved)
    # The one factory (E004-S01-003): it re-registers the passphrase and runs
    # the cipher's check() before the Run opens, so a backend failure is
    # rendered by the root and changes nothing.
    cipher = build_payload_cipher(
        passphrase=None if resolved is None else resolved.value, secrets=secrets
    )
    notes: tuple[str, ...] = ()
    if args.archive == LATEST:
        archive, note = _latest(
            Path(args.wp_dir),
            archive_dir=args.archive_dir,
            wp_config=args.wp_config,
            secrets=secrets,
        )
        notes = (note,)
    else:
        archive = storage_target_from_cli(args.archive)
    handle = _open_archive(archive)
    try:
        request = RestoreRequest(
            site_root=Path(args.wp_dir),
            archive=archive,
            mode=mode,
            overrides=overrides,
            mode_flags=render_flags(
                flag_combination(
                    mode=args.mode,
                    cleanfirst=args.cleanfirst,
                    drop_database=args.drop_database,
                    force=args.force,
                )
            ),
            snapshot=snapshot_policy_from_cli(
                no_safety_snapshot=args.no_safety_snapshot
            ),
            snapshot_dir=None if args.snapshot_dir is None else Path(args.snapshot_dir),
            config_policy=config_policy_from_cli(
                overwrite_wp_config=args.overwrite_wp_config
            ),
            resolve_run_id=args.resolve_interrupted_run,
            admin=admin_spec(
                username=admin_user,
                password=admin_password,
                aws_secret_id=args.admin_credentials_aws_secret_id,
                aws_region=args.admin_credentials_aws_region,
                environ=os.environ,
            ),
            cipher=cipher,
            extra_tools=() if cipher is None else cipher.required_tools(),
            archive_database=args.archive_database,
            db_tls=_db_tls(
                args.db_tls,
                args.db_tls_ca,
                admin_tls=args.admin_tls,
                admin_tls_ca=args.admin_tls_ca,
            ),
            relocation=relocation,
            reset_existing_db_user=bool(args.reset_existing_db_user),
            dry_run=bool(args.what_if),
            require_encrypted=bool(args.require_encrypted),
            allow_unencrypted=bool(args.allow_unencrypted),
            credentials=wordpress_spec(
                aws_secret_id=args.db_credentials_aws_secret_id,
                aws_region=args.db_credentials_aws_region,
            ),
            archive_handle=handle,
            notes=notes,
        )
        if privileges.running_as_root():
            # Owner decision 71: a configured administrator (an AWS profile in
            # root's home, say) is resolved before the switch, as a backup
            # resolves its WordPress account (owner decision 67).
            request = _admin_as_root(request, environ=os.environ, secrets=secrets)
        # Owner decision 58: everything only root may read has been read.
        privileges.drop_to_site_owner(
            Path(args.wp_dir), environ=os.environ, operation="restore"
        )
        try:
            outcome = drive_restore(
                request, ctx=None, secrets=secrets, environ=os.environ
            )
        except BaseException as exc:
            kept = _kept_snapshot(exc)
            if kept is not None:
                print_kept_snapshot(kept, error=exc)
            raise
    finally:
        handle.close()
    shown = {"archive": str(request.archive.path), "site_root": str(request.site_root)}
    try:
        require_database_restored(outcome, dry_run=outcome.dry_run)
    except BackupError:
        print_outcome(outcome, completed=False, **shown)
        raise
    print_outcome(outcome, completed=True, **shown)
    return ExitCode.OK
