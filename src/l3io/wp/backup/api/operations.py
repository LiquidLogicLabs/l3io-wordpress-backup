"""AD-16/AD-22 — the library's entry points: ``backup``, ``verify``, ``restore``.

Each is a composition root, as ``wpbackup2``'s ``run_*`` functions are, and
each does what its CLI twin does and nothing else: it translates its keyword
arguments into the frozen request the CLI builds, makes **one** call to the
driver the CLI calls (``drive_backup``, ``drive_verify``, ``drive_restore``),
and applies the CLI root's own exit rule to what the driver returns
(``VerificationFailedError``; ``require_database_restored``). It never
configures logging and never installs a signal handler (AD-10). It prints
nothing itself; ``drive_restore``'s safety notices go to the caller's
``on_warning`` or, without one, to stderr (AD-26 wants the
unauthenticated-archive warning outside the log; see **Notices**).

**One sequence (AD-16).** The drivers canonicalise the Site Root, open the
Run (a Run already active in this thread raises ``RunContextActiveError``;
there is no public way to join one, owner decision 2026-11-07, BL-E006-024),
take the Site Lock (AD-19), read the Site Descriptor from ``wp-config.php`` (AD-3) and
bind the adapter set — the Dry Run's no-op set for ``dry_run=True`` (AD-4).
Nothing here repeats any of those steps.

**Secrets (AD-26).** Each call creates one
``SecretRegistry`` before anything else, resolves the passphrase into it
through ``cli/passphrase.py``'s chain (the argument, then
``L3IO_WP_BACKUP_PASSPHRASE``, then ``prompt``; the library never binds a
terminal prompt itself), builds the cipher with
``adapters/crypto.build_payload_cipher`` and hands the registry to the
driver, whose Run adopts it. The
library reads ``os.environ`` only to hand it to that chain's module (the
chain itself, ``note_unused_passphrase`` for an unencrypted backup, and
``drive_verify`` when authenticating); the drivers hand it to the credential
builders.

**Tracing (AD-24).** Each call hands the driver an
``adapters/tracing.OpenTelemetryTracer``: inert unless the host configures an
OpenTelemetry SDK, and then one root span per Run and one span per
``phase_completed`` record, each nested in the span that encloses it (a
joined Run opens no root of its own). No command line is placed on a span
(``command_args=None``): a library call has none of its own. The CLI binds
the no-op tracer.

**Notices.** Every entry point's operator notices go to ``on_warning`` as
``RunWarning`` values when it is given, and to stderr when it is not; their
WARNING log records are written either way, and an exception the callback
raises propagates (owner decision 2026-11-07, BL-E008-012). ``restore``'s: an
unauthenticated Archive, the Safety Snapshot's encryption, a passphrase below
the encryption floor, running as root. ``backup``'s: a passphrase variable set
without ``encrypt=True``, a target inside the Site Root. ``verify`` gives none
today.

**Errors (AD-14).** Each entry point is wrapped in ``api/errors.py``'s
``public_boundary``: a package exception passes through unchanged, and any
other ``Exception`` arrives as ``UnexpectedError`` from the original.

Pruning is FR-17 (v1.1): the CLI has no prune operation, so neither has this.
"""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Callable
from pathlib import Path
from typing import Final

from l3io.wp.backup.adapters.crypto import build_payload_cipher
from l3io.wp.backup.adapters.exclusions import LoggedExclusionPolicy
from l3io.wp.backup.adapters.preflight import Diagnosis
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.adapters.tracing import OpenTelemetryTracer
from l3io.wp.backup.api.errors import public_boundary
from l3io.wp.backup.cli.backup import (
    NO_ENVIRONMENT,
    BackupRequest,
    BackupResult,
    default_exclusion_policy,
    drive_backup,
    storage_target_from_cli,
)
from l3io.wp.backup.cli.credentials import new_db_password
from l3io.wp.backup.cli.doctor import drive_doctor
from l3io.wp.backup.cli.output import WarningSink
from l3io.wp.backup.cli.passphrase import (
    note_short_restore_passphrase,
    note_unused_passphrase,
    require_passphrase,
    resolve_passphrase,
)
from l3io.wp.backup.cli.restore import RESTORE_PROMPT, drive_restore
from l3io.wp.backup.cli.restore_report import require_database_restored
from l3io.wp.backup.cli.restore_request import (
    RelocationFlags,
    RestoreOutcome,
    RestoreRequest,
)
from l3io.wp.backup.cli.verify import VerifyRequest, drive_verify
from l3io.wp.backup.core.exclusions import ExclusionSet
from l3io.wp.backup.domain.archive_format import VerificationResult
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.credentials import CredentialSpec
from l3io.wp.backup.domain.errors import UsageError, VerificationFailedError
from l3io.wp.backup.domain.preflight import ToolRequirement
from l3io.wp.backup.domain.restore import (
    ConfigPolicy,
    Override,
    OverrideSet,
    RestoreMode,
    SnapshotPolicy,
)
from l3io.wp.backup.domain.secret import Secret
from l3io.wp.backup.domain.storage_target import StorageTarget
from l3io.wp.backup.ports.crypto import PayloadCipher
from l3io.wp.backup.ports.database import DbTls
from l3io.wp.backup.ports.exclusion import ExclusionPolicy

__all__ = ["SNAPSHOT_SKIPPED_BY", "backup", "doctor", "restore", "verify"]

#: What a library Run's ``safety_snapshot_skipped`` record names as having
#: chosen ``SnapshotPolicy.SKIP`` (BL-E003-050): the argument, not the CLI flag.
SNAPSHOT_SKIPPED_BY: Final = "snapshot=SnapshotPolicy.SKIP"
#: The library's name for a passphrase its caller passes, which a
#: ``PassphraseUnavailableError`` hint names (UX-01-17; the CLI takes none).
_PASSPHRASE_ARGUMENT: Final = "passphrase"  # noqa: S105 - a parameter name


def _registry() -> SecretRegistry:
    """A new registry, which the Run the driver opens adopts."""
    return SecretRegistry()


def _target(
    value: StorageTarget | str | os.PathLike[str], *, argument: str
) -> StorageTarget:
    """A ``StorageTarget`` unchanged; text or a path through the CLI's one grammar.

    ``argument`` is the parameter a refusal names (BL-E006-015).
    """
    if isinstance(value, StorageTarget):
        return value
    return storage_target_from_cli(os.fspath(value), argument=argument)


def _require(value: object, expected: type, argument: str) -> None:
    """``UsageError`` naming ``argument`` unless ``value`` is an ``expected``.

    BL-E006-018: a wrong-typed closed choice (``scope="all"``, a bare string
    for ``mode``) is the caller's mistake, refused at the API edge, not a
    defect surfacing later as ``UnexpectedError``. The message names the
    types, never the value.
    """
    if not isinstance(value, expected):
        msg = f"{argument} must be a {expected.__name__}, not {type(value).__name__}"
        raise UsageError(msg, arguments=(argument,))


def _require_overrides(overrides: object) -> None:
    """``overrides`` is an ``OverrideSet`` of ``Override`` members only."""
    _require(overrides, OverrideSet, "overrides")
    waived = getattr(overrides, "waived", None)
    if not isinstance(waived, frozenset) or not all(
        isinstance(item, Override) for item in waived
    ):
        msg = "overrides must waive Override members only"
        raise UsageError(msg, arguments=("overrides",))


def _exclusion(policy: ExclusionPolicy) -> ExclusionPolicy:
    """An ``ExclusionSet`` wrapped so its decisions are logged, as the CLI's are."""
    if isinstance(policy, ExclusionSet):
        return LoggedExclusionPolicy(policy)
    return policy


@public_boundary
def backup(
    site: str | os.PathLike[str],
    *,
    scope: BackupScope,
    target: StorageTarget | str | os.PathLike[str],
    wp_config: str | os.PathLike[str] | None = None,
    credentials: CredentialSpec | None = None,
    exclusion: ExclusionPolicy | None = None,
    encrypt: bool = False,
    passphrase: Secret | str | None = None,
    prompt: Callable[[str], str] | None = None,
    dry_run: bool = False,
    on_warning: WarningSink | None = None,
    db_tls: DbTls | None = None,
) -> BackupResult:
    """Back up the Site whose root is ``site``; returns what was published.

    ``scope`` and ``target`` have no default (AD-5; owner decision
    2026-11-07): the CLI's default Archive name is the CLI's alone.
    ``exclusion=None`` is the set the CLI applies with no exclusion flag.
    ``encrypt=True`` requires a passphrase
    (``passphrase``, ``L3IO_WP_BACKUP_PASSPHRASE``, then ``prompt``) and
    refuses with ``PassphraseUnavailableError`` before the Site is read; a
    ``passphrase`` without ``encrypt=True`` is refused (``UsageError``) rather
    than ignored, so an Archive is never written plain by omission, and a
    ``L3IO_WP_BACKUP_PASSPHRASE`` set without it is logged as the CLI logs it
    (``passphrase_ignored``).
    ``dry_run=True`` plans, checks and writes nothing.
    The database host is logged (INFO ``database_host``), never confirmed
    (owner decision 54); a ``target`` inside the Site Root is written with
    WARNING ``archive_target_in_site_root`` through ``on_warning`` (owner
    decision 60). ``db_tls`` requires TLS on the database connections, as
    ``--db-tls``/``--db-tls-ca``; ``None`` requires none (owner decision 56).
    A ``scope`` that is not a ``BackupScope`` is refused (``UsageError``).
    """
    _require(scope, BackupScope, "scope")
    registry = _registry()
    cipher: PayloadCipher | None = None
    extra_tools: tuple[ToolRequirement, ...] = ()
    if encrypt:
        resolved = require_passphrase(
            explicit=passphrase,
            environ=os.environ,
            prompt=prompt,
            secrets=registry,
            argument=_PASSPHRASE_ARGUMENT,
        )
        built = build_payload_cipher(passphrase=resolved.value, secrets=registry)
        cipher, extra_tools = built, built.required_tools()
    elif passphrase is not None:
        msg = "a passphrase was given but encrypt=True was not: nothing would use it"
        raise UsageError(msg, arguments=("passphrase", "encrypt"))
    else:
        note_unused_passphrase(os.environ, on_warning)
    site_root = Path(site)
    stated = None if wp_config is None else Path(wp_config)
    archive = _target(target, argument="target")
    request = BackupRequest(
        site_root=site_root,
        scope=scope,
        target=archive,
        wp_config=stated,
        credentials=credentials,
        cipher=cipher,
        exclusion=(
            default_exclusion_policy() if exclusion is None else _exclusion(exclusion)
        ),
        dry_run=dry_run,
        extra_tools=extra_tools,
        db_tls=DbTls() if db_tls is None else db_tls,
        target_chosen_by="target",
    )
    return drive_backup(
        request,
        ctx=None,
        tracer=OpenTelemetryTracer(),
        secrets=registry,
        command_args=None,
        on_warning=on_warning,
        environ=os.environ,
    )


@public_boundary
def verify(
    archive: StorageTarget | str | os.PathLike[str],
    *,
    authenticate: bool = False,
    passphrase: Secret | str | None = None,
    prompt: Callable[[str], str] | None = None,
    on_warning: WarningSink | None = None,
) -> VerificationResult:
    """Check ``archive`` without touching a Site; returns the passing result.

    Keyless by default: a pass proves the checksums are consistent, not that
    the Archive is authentic (ADR-0024). ``authenticate=True`` resolves the
    passphrase and decrypts the payload, as ``verify --authenticate`` does; a
    ``passphrase`` without ``authenticate=True`` is refused (``UsageError``)
    rather than ignored, as ``backup`` refuses one without ``encrypt=True``.
    A failed verification raises ``VerificationFailedError`` carrying the
    result, as the CLI exits ``VERIFICATION_FAILED``.
    """
    if passphrase is not None and not authenticate:
        msg = (
            "a passphrase was given but authenticate=True was not: nothing would use it"
        )
        raise UsageError(msg, arguments=("passphrase", "authenticate"))
    registry = _registry()
    request = VerifyRequest(
        archive=_target(archive, argument="archive"), authenticate=authenticate
    )
    result = drive_verify(
        request,
        ctx=None,
        tracer=OpenTelemetryTracer(),
        secrets=registry,
        command_args=None,
        passphrase=passphrase,
        passphrase_argument=_PASSPHRASE_ARGUMENT,
        environ=os.environ if authenticate else NO_ENVIRONMENT,
        prompt=prompt,
        on_warning=on_warning,
    )
    if not result.passed:
        raise VerificationFailedError(result, str(request.archive.path))
    return result


@public_boundary
def restore(
    site_root: str | os.PathLike[str],
    *,
    archive: StorageTarget | str | os.PathLike[str],
    mode: RestoreMode,
    overrides: OverrideSet,
    snapshot: SnapshotPolicy,
    config_policy: ConfigPolicy,
    snapshot_dir: str | os.PathLike[str] | None = None,
    admin: CredentialSpec | None = None,
    db_tls: DbTls | None = None,
    archive_database: str | None = None,
    reset_existing_db_user: bool = False,
    relocation: RelocationFlags | None = None,
    require_encrypted: bool = False,
    allow_unencrypted: bool = False,
    passphrase: Secret | str | None = None,
    prompt: Callable[[str], str] | None = None,
    resolve_run_id: str | None = None,
    dry_run: bool = False,
    on_warning: WarningSink | None = None,
) -> RestoreOutcome:
    """Restore ``archive`` into the Site at ``site_root``; returns what was done.

    ``mode``, ``overrides``, ``snapshot`` and ``config_policy`` have no
    defaults (AD-5); FR-7's guard runs whichever root called. The passphrase
    is resolved on every call: it decrypts an encrypted Archive and encrypts
    the Safety Snapshot. ``db_tls=None`` requires no TLS, as WordPress
    connects (owner decision 56); ``relocation=None`` is no relocation. A
    plain Archive restored with a passphrase (``passphrase``,
    ``L3IO_WP_BACKUP_PASSPHRASE`` or ``prompt``) proceeds with WARNING
    ``archive_not_encrypted``; ``require_encrypted=True`` refuses it (owner
    decision 59). ``allow_unencrypted`` is deprecated and has no effect. A
    real restore whose database report does not say restored raises
    ``DatabaseNotRestoredError``, as the CLI exits ``FAILED``. A process
    running as root is refused (``RootRestoreRefusedError``): the library
    never switches its host's user, as the CLI does (owner decision 58).
    A ``mode``, ``overrides``, ``snapshot`` or ``config_policy`` of the wrong
    type is refused (``UsageError`` naming the parameter) before any effect.
    """
    _require(mode, RestoreMode, "mode")
    _require_overrides(overrides)
    _require(snapshot, SnapshotPolicy, "snapshot")
    _require(config_policy, ConfigPolicy, "config_policy")
    registry = _registry()
    flags = RelocationFlags.none() if relocation is None else relocation
    # The CLI's chain without its file flag: the argument, then
    # L3IO_WP_BACKUP_NEW_DB_PASSWORD (only with new_db_user); registered and
    # held to the ADR-0018 floor as it is read.
    password = new_db_password(
        explicit=flags.new_db_password,
        file=None,
        environ=os.environ,
        secrets=registry,
        user_given=flags.new_db_user is not None,
        explicit_source="RelocationFlags.new_db_password",
    )
    if password != flags.new_db_password:
        flags = dataclasses.replace(flags, new_db_password=password)
    resolved = resolve_passphrase(
        explicit=passphrase,
        environ=os.environ,
        prompt=prompt,
        secrets=registry,
        label=RESTORE_PROMPT,
        argument=_PASSPHRASE_ARGUMENT,
    )
    note_short_restore_passphrase(resolved, on_warning)
    cipher = build_payload_cipher(
        passphrase=None if resolved is None else resolved.value, secrets=registry
    )
    request = RestoreRequest(
        site_root=Path(site_root),
        archive=_target(archive, argument="archive"),
        mode=mode,
        overrides=overrides,
        snapshot=snapshot,
        snapshot_dir=None if snapshot_dir is None else Path(snapshot_dir),
        config_policy=config_policy,
        resolve_run_id=resolve_run_id,
        admin=admin,
        cipher=cipher,
        extra_tools=() if cipher is None else cipher.required_tools(),
        archive_database=archive_database,
        db_tls=DbTls() if db_tls is None else db_tls,
        relocation=flags,
        reset_existing_db_user=reset_existing_db_user,
        dry_run=dry_run,
        opted_out_by=SNAPSHOT_SKIPPED_BY,
        require_encrypted=require_encrypted,
        allow_unencrypted=allow_unencrypted,
    )
    outcome = drive_restore(
        request,
        ctx=None,
        tracer=OpenTelemetryTracer(),
        secrets=registry,
        command_args=None,
        on_warning=on_warning,
        environ=os.environ,
    )
    require_database_restored(outcome, dry_run=outcome.dry_run)
    return outcome


@public_boundary
def doctor(
    *,
    site: str | os.PathLike[str] | None = None,
    archive: StorageTarget | str | os.PathLike[str] | None = None,
) -> Diagnosis:
    """FR-27: whether this host is ready, as ``wpbackup2 doctor`` reports it.

    Returns the ``Diagnosis`` (``ready``, the tools found, the platform, the
    extras, the passphrase backend by name, the archive target and, with
    ``site``, that Site's dump readiness); it raises nothing for what it
    reports. Changes nothing and takes no Site Lock.
    """
    return drive_doctor(
        site_root=None if site is None else Path(site),
        target=None if archive is None else _target(archive, argument="archive"),
        secrets=SecretRegistry(),
        tracer=OpenTelemetryTracer(),
        environ=os.environ,
    )
