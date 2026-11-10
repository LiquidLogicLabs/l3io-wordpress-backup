"""FR-6, FR-7, AD-25 — a restore's values: the request, the outcome, the flags.

Split from ``cli/restore.py`` (BL-E003-109). What a caller hands
``drive_restore`` (:class:`RestoreRequest`, with Epic 7's
:class:`RelocationFlags` and its :class:`RestoreExtension` seam) and what it
returns (:class:`RestoreOutcome`); the restore flags read into a Mode and its
overrides (AD-25's table) and rendered back in the operator's spelling for a
refusal (:class:`NonEmptyTargetRefusal`); and the Archive's database identity
the operator confirms (``--archive-database``, BL-E002-114).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Final, Protocol, cast

import structlog

from l3io.wp.backup.adapters.database import (
    database_location_from_db_host,
)
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.cli.credentials import (
    admin_spec,
    wordpress_spec,
)
from l3io.wp.backup.cli.scope import CLI_MODE_TO_SCOPE, backup_scope_from_cli
from l3io.wp.backup.core.restore import (
    DatabaseRestoreReport,
    NonEmptyTargetError,
    PlannedRestoreAction,
    RestoreRelocation,
)
from l3io.wp.backup.domain.archive_format import (
    ArchiveView,
    Integrity,
    SkippedLink,
)
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.credentials import CredentialSpec, ResolvedCredentialSpec
from l3io.wp.backup.domain.database import DatabaseLocation
from l3io.wp.backup.domain.errors import (
    RefusedError,
    UsageError,
)
from l3io.wp.backup.domain.flag_map import (
    FLAG_MAP,
    FlagCombination,
    FlagRejection,
    combination,
    resolve_flags,
)
from l3io.wp.backup.domain.preflight import ToolRequirement
from l3io.wp.backup.domain.restore import (
    ConfigPolicy,
    OverrideSet,
    RestoreMode,
    RestorePhase,
    SnapshotPolicy,
)
from l3io.wp.backup.domain.search_replace import SearchReplaceReport
from l3io.wp.backup.domain.secret import Secret, as_secret
from l3io.wp.backup.domain.site import SiteDescriptor
from l3io.wp.backup.domain.storage_target import StorageTarget
from l3io.wp.backup.ports.crypto import PayloadCipher
from l3io.wp.backup.ports.database import DbTls
from l3io.wp.backup.ports.search_replace import SearchReplaceCommand
from l3io.wp.backup.ports.site_files import SiteFilesQuery

__all__ = [
    "NO_SNAPSHOT_FLAG",
    "ArchiveIdentityUnconfirmedError",
    "ExistingDatabaseUserError",
    "NonEmptyTargetRefusal",
    "RelocationFlags",
    "RestoreExtension",
    "RestoreExtensionFactory",
    "RestoreOutcome",
    "RestoreRequest",
    "check_restore_arguments",
    "config_policy_from_cli",
    "confirm_identity",
    "flag_combination",
    "parse_archive_database",
    "refusal_for",
    "render_flags",
    "restore_mode_from_cli",
    "snapshot_policy_from_cli",
]

_log = structlog.get_logger(__name__)

#: What opts out of the Safety Snapshot on the command line (BL-E003-050).
NO_SNAPSHOT_FLAG: Final = "--no-safety-snapshot"
_SCOPE_TO_CLI: Final = {scope: text for text, scope in CLI_MODE_TO_SCOPE.items()}


def flag_combination(
    *, mode: str, cleanfirst: bool, drop_database: bool, force: bool
) -> FlagCombination:
    """The AD-25 table key for the restore flags' parsed values."""
    return combination(
        mode=backup_scope_from_cli(mode),
        cleanfirst=cleanfirst,
        drop_database=drop_database,
        force=force,
    )


def restore_mode_from_cli(
    *, mode: str, cleanfirst: bool, drop_database: bool, force: bool
) -> tuple[RestoreMode, OverrideSet]:
    """The Mode and waivers the flags express; raises ``USAGE`` otherwise."""
    return resolve_flags(
        flag_combination(
            mode=mode, cleanfirst=cleanfirst, drop_database=drop_database, force=force
        )
    )


def snapshot_policy_from_cli(*, no_safety_snapshot: bool) -> SnapshotPolicy:
    """``--no-safety-snapshot`` is the explicit opt-out; the default takes one."""
    return SnapshotPolicy.SKIP if no_safety_snapshot else SnapshotPolicy.TAKE


def config_policy_from_cli(*, overwrite_wp_config: bool) -> ConfigPolicy:
    """``--overwrite-wp-config`` may replace the target's ``wp-config.php``."""
    return ConfigPolicy.OVERWRITE if overwrite_wp_config else ConfigPolicy.KEEP


def check_restore_arguments(
    *,
    mode: str,
    cleanfirst: bool,
    drop_database: bool,
    force: bool,
    admin_username: str | None,
    admin_password: str | None,
    admin_aws_secret_id: str | None,
    admin_aws_region: str | None,
    db_aws_secret_id: str | None,
    db_aws_region: str | None,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Raise ``UsageError`` for restore arguments no Run could act on.

    The flag combination must express a Restore Mode, and each credential
    flag pair must be whole. Nothing outside the arguments is read.
    """
    restore_mode_from_cli(
        mode=mode, cleanfirst=cleanfirst, drop_database=drop_database, force=force
    )
    admin_spec(
        username=admin_username,
        password=admin_password,
        aws_secret_id=admin_aws_secret_id,
        aws_region=admin_aws_region,
        environ=environ,
    )
    wordpress_spec(aws_secret_id=db_aws_secret_id, aws_region=db_aws_region)


# --------------------------------------------------------------------------
# The refusal's flag text (BL-E003-039, UX-1, UX-2)
# --------------------------------------------------------------------------


def _cli_flag(dest: str) -> str:
    return "--" + dest.replace("_", "-")


def render_flags(flags: FlagCombination) -> str:
    """``flags`` as the operator types them: ``--mode db --cleanfirst --force``.

    ``mode`` through the inverse of ``CLI_MODE_TO_SCOPE`` (so ``database``
    reads ``db``), then each true boolean dimension as ``--<dest>``; a false
    one is omitted. ``core/``'s domain spelling is never shown to an operator.
    """
    valued: list[str] = []
    for dest, value in flags:
        if isinstance(value, bool):
            continue
        text = _SCOPE_TO_CLI[value] if isinstance(value, BackupScope) else str(value)
        valued.append(f"{_cli_flag(dest)} {text}")
    switches = [_cli_flag(dest) for dest, value in flags if value is True]
    return " ".join(valued + switches)


def _mode_flags(mode: RestoreMode) -> str:
    """The fewest flags that select ``mode`` with nothing waived (UX-2)."""
    selecting = [
        flags
        for flags, entry in FLAG_MAP.items()
        if not isinstance(entry, FlagRejection)
        and entry[0] is mode
        and entry[1] == OverrideSet.none()
    ]
    return render_flags(min(selecting, key=lambda f: sum(v is True for _, v in f)))


class NonEmptyTargetRefusal(NonEmptyTargetError):
    """FR-7's refusal, worded in the flags the operator types (BL-E003-039).

    The same fields as :class:`NonEmptyTargetError`; ``flags`` is the CLI
    spelling of ``authorizing``, and ``original`` the refusal it rewords.
    Names paths and ``host:port/name`` only.
    """

    def __init__(self, original: NonEmptyTargetError) -> None:
        self.original = original
        super().__init__(
            original.mode,
            parts=original.parts,
            findings=original.findings,
            authorizing=original.authorizing,
        )
        self.flags = render_flags(original.authorizing)
        named = "; ".join(f"{where} ({what})" for where, what in original.findings)
        self.args = (
            f"refusing to restore with {_mode_flags(original.mode)} (Restore Mode "
            f"{original.mode.value}) over a non-empty target "
            f"({' and '.join(original.parts)}): found {named}; nothing was "
            f"changed. {self.flags} would authorize it",
        )


def refusal_for(exc: NonEmptyTargetError) -> NonEmptyTargetRefusal:
    refusal = NonEmptyTargetRefusal(exc)
    _log.error(
        "restore_refused",
        reason="non_empty_target",
        parts=list(exc.parts),
        found=exc.found,
        detail=exc.detail,
        findings=[list(finding) for finding in exc.findings],
        authorizing=refusal.flags,
        restore_mode=exc.mode.value,
    )
    return refusal


# --------------------------------------------------------------------------
# BL-E002-114: the Archive's database identity, confirmed by the operator
# --------------------------------------------------------------------------


class ArchiveIdentityUnconfirmedError(RefusedError):
    """The target has no ``wp-config.php``, the Archive is not encrypted, and
    ``--archive-database`` did not name the Archive config's database (RT-1).
    An encrypted Archive is authenticated by its passphrase, so its config's
    database is used without it (owner decision 65).

    Raised after the bind and before any database connection, so a forged
    Archive cannot direct the administrator to a server the operator did not
    name. Never a credential.

    RT-S02-003 (owner decision, 2026-11-06, option A): when the flag is
    absent the refusal does **not** show the Archive's database, so the
    operator must name the server from their own knowledge instead of
    pasting the Archive's claim back. Only when the operator's value differs
    is the Archive's database shown, with which parts differ, and never as a
    ready-to-paste flag.
    """

    def __init__(
        self,
        *,
        user: str,
        location: DatabaseLocation,
        differs: tuple[str, ...] | None,
    ) -> None:
        self.user = user
        self.location = location
        self.differs = differs
        if differs is None:
            super().__init__(
                "the target has no wp-config.php, so the database would come "
                "from the archive's config. Name the database this site is "
                "being restored to, from your own records, with "
                "--archive-database USER@HOST[:PORT]/NAME; it must match the "
                "archive's. Nothing was changed"
            )
            return
        named = (
            f"{user} at {location.host} port {location.port}, database {location.name}"
        )
        # L-4: the operator's text is never echoed; only which part differs.
        super().__init__(
            "the target has no wp-config.php, and --archive-database names "
            f"another {', '.join(differs)} than the archive's config ({named}). "
            "If that is not the server you meant, do not restore this archive "
            "without checking where it came from. Nothing was changed"
        )


class ExistingDatabaseUserError(RefusedError):
    """The Archive's config names a database account that already exists.

    RT-S02-004 (owner decision, 2026-11-06, option A): with no target
    ``wp-config.php`` the WordPress account and its password come from the
    Archive, and provisioning would reset an existing account's password to
    the Archive's value. That is refused unless the operator passes
    ``--reset-existing-db-user``. A missing account is still created. Raised
    after the administrator's survey and before any effect; never a password.
    """

    def __init__(self, *, user: str, location: DatabaseLocation) -> None:
        self.user = user
        self.location = location
        super().__init__(
            f"the archive's config names the database account {user}, which "
            f"already exists on {location.host}:{location.port}; restoring "
            "would reset its password to the archive's. Pass "
            "--reset-existing-db-user if that account belongs to this site. "
            "Nothing was changed"
        )


def parse_archive_database(text: str) -> tuple[str, DatabaseLocation]:
    """``USER@HOST[:PORT]/NAME`` → ``(user, location)``; ``UsageError`` otherwise.

    Split at the first ``@`` and the last ``/``; ``HOST[:PORT]`` is parsed by
    ``adapters/database.py``'s ``DB_HOST`` parsing, never a second way. The
    text is never echoed (L-4): an operator may type ``USER:PASSWORD@…`` out
    of URL habit, so a ``:`` in the user part is refused without showing it.
    """
    user, at, rest = text.partition("@")
    host, slash, name = rest.rpartition("/")
    if not (at and user and slash and host and name):
        msg = "--archive-database: expected USER@HOST[:PORT]/NAME"
        raise UsageError(msg, arguments=("archive_database",))
    if ":" in user:
        msg = (
            "--archive-database: the user part holds ':'; give USER@HOST[:PORT]/NAME "
            "with no password"
        )
        raise UsageError(msg, arguments=("archive_database",))
    return user, database_location_from_db_host(host, name)


def confirm_identity(stated: str | None, *, site: SiteDescriptor, user: str) -> None:
    """Refuse unless ``stated`` names exactly the Archive config's database."""
    location = site.database_location()
    differs: tuple[str, ...] | None = None
    if stated is not None:
        stated_user, named = parse_archive_database(stated)
        differs = tuple(
            part
            for part, same in (
                ("user", stated_user == user),
                ("host", named.host.lower() == location.host.lower()),
                ("port", named.port == location.port),
                ("database", named.name == location.name),
            )
            if not same
        )
        if not differs:
            _log.info(
                "archive_database_confirmed",
                db_host=location.host,
                db_port=location.port,
                db_name=location.name,
                db_user=user,
            )
            return
    raise ArchiveIdentityUnconfirmedError(user=user, location=location, differs=differs)


# --------------------------------------------------------------------------
# Epic 7's seam (decision 2 of 2026-10-27)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class RelocationFlags:
    """The relocation flags, copied verbatim; Epic 7 validates them.

    ``new_db_password`` is left out of ``repr`` (and registered with the Run's
    ``SecretRegistry`` before anything uses it). The CLI fills it from
    ``--new-db-password``, ``--new-db-password-file`` or
    ``L3IO_WP_BACKUP_NEW_DB_PASSWORD``, in that order; the library from this
    field, then that variable (only with ``new_db_user``), through
    ``cli/credentials.new_db_password``.
    """

    new_site_url: str | None
    new_home_url: str | None
    new_db_host: str | None
    new_db_name: str | None
    new_db_user: str | None
    new_db_password: Secret | str | None = field(repr=False)
    wp_config: Path | None

    def __post_init__(self) -> None:
        # Owner decision 2026-11-07: a str is held as a Secret at once.
        if self.new_db_password is not None:
            object.__setattr__(self, "new_db_password", as_secret(self.new_db_password))

    @classmethod
    def none(cls) -> RelocationFlags:
        """No relocation flag given."""
        return cls(
            new_site_url=None,
            new_home_url=None,
            new_db_host=None,
            new_db_name=None,
            new_db_user=None,
            new_db_password=None,
            wp_config=None,
        )

    @property
    def given(self) -> tuple[str, ...]:
        """The names of the fields that were set."""
        return tuple(
            f.name
            for f in dataclasses.fields(self)
            if getattr(self, f.name) is not None
        )

    @property
    def is_empty(self) -> bool:
        return not self.given


class RestoreExtension(Protocol):
    """What Epic 7 supplies to a restore; declared here, implemented there."""

    def required_tools(self) -> tuple[ToolRequirement, ...]: ...

    def wordpress_spec(self) -> CredentialSpec | None: ...

    def search_replace(self) -> SearchReplaceCommand | None: ...

    def resolve(
        self,
        *,
        site_root: Path,
        site: SiteDescriptor,
        view: ArchiveView,
        files: SiteFilesQuery,
        mode: RestoreMode,
    ) -> RestoreRelocation: ...


class RestoreExtensionFactory(Protocol):
    """``adapters/restore_extension.py::restore_extension``'s signature."""

    def __call__(
        self, flags: RelocationFlags, *, secrets: SecretRegistry
    ) -> RestoreExtension: ...


# --------------------------------------------------------------------------
# The request and the outcome
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class RestoreRequest:
    """Everything a restore needs from its caller; :func:`drive_restore` does the rest.

    ``mode``, ``overrides``, ``snapshot`` and ``config_policy`` have no
    defaults (AD-5). ``snapshot_dir=None`` is the per-Site default under the
    per-user state directory. ``archive_database`` is ``--archive-database``'s
    raw ``USER@HOST[:PORT]/NAME``, needed only for an unencrypted Archive on a
    target with no ``wp-config.php`` (owner decision 65), and ``db_tls`` is
    ``--db-tls``/``--db-tls-ca`` (owner decision 56; the default requires no
    TLS). ``opted_out_by`` names what chose ``SnapshotPolicy.SKIP``
    (BL-E003-050). ``credentials`` is the WordPress account's spec
    (``--db-credentials-aws-*``); ``None`` means ``wp-config.php``. A plain
    Archive restored with a ``cipher`` (a passphrase was given) proceeds with
    WARNING ``archive_not_encrypted``; ``require_encrypted`` refuses it (owner
    decision 59). ``allow_unencrypted`` is deprecated and changes nothing.
    ``archive_handle`` is the Archive already opened by a root that then
    switched users (owner decision 58), read instead of opening
    ``archive.path``; the root closes it. ``notes`` are lines the root adds
    to the outcome's (``--archive latest``'s choice). ``admin`` is a
    ``ResolvedCredentialSpec`` only when the CLI, started as root, resolved
    the administrator before it switched users (owner decision 71). No field
    carries an ``l3io.wp.database`` type (ADR-0015 amendment).
    """

    site_root: Path
    archive: StorageTarget
    mode: RestoreMode
    overrides: OverrideSet
    snapshot: SnapshotPolicy
    snapshot_dir: Path | None
    config_policy: ConfigPolicy
    resolve_run_id: str | None
    admin: CredentialSpec | ResolvedCredentialSpec | None
    cipher: PayloadCipher | None
    archive_database: str | None
    relocation: RelocationFlags
    reset_existing_db_user: bool
    dry_run: bool
    extra_tools: tuple[ToolRequirement, ...] = ()
    opted_out_by: str = NO_SNAPSHOT_FLAG
    require_encrypted: bool = False
    credentials: CredentialSpec | None = None
    allow_unencrypted: bool = False
    db_tls: DbTls = field(default_factory=DbTls)
    archive_handle: BinaryIO | None = field(default=None, repr=False, compare=False)
    notes: tuple[str, ...] = ()
    #: The flags that produced ``mode`` and ``overrides`` as the operator typed
    #: them (``--mode all --cleanfirst --drop-database``), logged on
    #: ``restore_mode_resolved`` (FR-6, BL-E003-013); ``None`` for a library
    #: call, which passes the Mode itself.
    mode_flags: str | None = None

    def __post_init__(self) -> None:
        if not self.opted_out_by:
            msg = "opted_out_by must name what may skip the Safety Snapshot"
            raise ValueError(msg)
        if not isinstance(cast("object", self.db_tls), DbTls):
            msg = "db_tls must be a DbTls; DbTls() requires no TLS"
            raise TypeError(msg)


@dataclass(frozen=True, slots=True, kw_only=True)
class RestoreOutcome:
    """What one restore did (or, in a Dry Run, planned); built, never printed.

    ``resolved_run_id`` is set when the Run only resolved an interrupted
    journal (no phase ran); ``integrity`` is then ``None``.

    ``skipped_links`` are the symlinks extraction skipped; in a Dry Run, the
    ones the real Run would skip (owner decision 41), and ``None`` when the
    Archive records no link targets (v0), whose skipped links are reported
    only at extraction.

    ``notes`` are what the Run chose for the operator without asking, each
    also logged: the Archive ``--archive latest`` named (decision 64) and an
    encrypted Archive's database used as its config gives it (decision 65).
    A Dry Run prints them before its plan.

    ``wp_config_absent`` is true when the target had no ``wp-config.php``
    and the Run wrote none: a database-only restore onto such a folder
    (owner decision 74).
    """

    run_id: str
    mode: RestoreMode
    phases: tuple[RestorePhase, ...]
    dry_run: bool
    actions: tuple[PlannedRestoreAction, ...]
    integrity: Integrity | None
    snapshot_path: PurePosixPath | None
    snapshot_encrypted: bool
    database: DatabaseRestoreReport | None
    search_replace: SearchReplaceReport | None
    resolved_run_id: str | None
    skipped_links: tuple[SkippedLink, ...] | None
    wp_config_kept: bool
    wp_config_absent: bool = False
    notes: tuple[str, ...] = ()
