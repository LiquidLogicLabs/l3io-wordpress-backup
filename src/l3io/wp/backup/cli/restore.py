"""``wpbackup2 restore`` — the restore root (E003-S02-006).

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
- The executor, the plan, the staging cutover and the journal are Epic 3's
  (``core/restore``, ``adapters/{restore_runner,staging,journal}``); this root
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
one place the CLI's ``db`` becomes ``database``; :func:`render_flags` is its
inverse for refusals (BL-E003-039).

FR-9's database report (E003-S02-004) is rendered here for the root to print:
:func:`database_report_lines` (human), :func:`database_report_json` (one JSON
object, for ``--log-format json``), :func:`emit_database_report` choosing
between them, and :func:`require_database_restored`, the exit rule. Under
``--log-format json`` stdout is instead one object, :func:`restore_summary`,
holding the report among everything else; a text-mode Run that completed ends
with :func:`completion_line` (UX-S02-04).
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import functools
import json
import os
import shutil
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC
from pathlib import Path, PurePath, PurePosixPath
from typing import Final, Protocol, cast

import structlog

from l3io.wp.backup.adapters.crypto import PassphraseCipher
from l3io.wp.backup.adapters.database import (
    AdminTls,
    LocatedConfig,
    check_admin_tls,
    database_location_from_db_host,
    locate_wp_config,
    password_at_call_time,
    site_descriptor_from_archive_config,
    site_descriptor_from_wp_config,
)
from l3io.wp.backup.adapters.journal import (
    CURRENT,
    FileRunJournal,
    journal_directory,
)
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.adapters.restore_runner import (
    CONFIG_MAX_BYTES,
    config_member,
    execute_restore,
    snapshot_result,
)
from l3io.wp.backup.adapters.staging import (
    check_staged,
    complete_cutover,
    load_staged,
)
from l3io.wp.backup.adapters.tar import TarArchiveProbe, decrypt_payload
from l3io.wp.backup.cli.adapter_sets import (
    dry_run_adapters,
    real_adapters,
    real_site_lock,
)
from l3io.wp.backup.cli.backup import backup, storage_target_from_cli
from l3io.wp.backup.cli.context import RunContext, join_or_open
from l3io.wp.backup.cli.credentials import (
    admin_source,
    admin_spec,
    wordpress_source,
    wordpress_spec,
)
from l3io.wp.backup.cli.logging import configured_format
from l3io.wp.backup.cli.output import emit_result
from l3io.wp.backup.cli.passphrase import resolve_passphrase, terminal_prompt
from l3io.wp.backup.cli.preflight import DumpReadinessPreflight, require_tools
from l3io.wp.backup.cli.scope import CLI_MODE_TO_SCOPE, backup_scope_from_cli
from l3io.wp.backup.core.restore import (
    FREE_SPACE_MARGIN,
    DatabaseRestoreReport,
    InsufficientSpaceError,
    NonEmptyTargetError,
    PlannedRestoreAction,
    RestoreInputs,
    RestorePlan,
    RestoreRelocation,
    SnapshotResult,
    TargetState,
    build_restore_plan,
    database_report_fields,
    incomplete_run_error,
    load_names,
    planned_cutover_keys,
    planned_cutover_targets,
)
from l3io.wp.backup.domain.archive_format import (
    ArchiveView,
    Integrity,
    ManifestOrigin,
    OriginKind,
    SkippedLink,
    classify,
)
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.credentials import CredentialSpec
from l3io.wp.backup.domain.database import DatabaseLocation, DatabaseTargetState
from l3io.wp.backup.domain.errors import (
    ArchiveNotFoundError,
    ArchiveWriteError,
    BackupError,
    InvalidSiteDescriptorError,
    PassphraseRequiredError,
    RefusedError,
    UsageError,
)
from l3io.wp.backup.domain.exclusion import INCLUDE_EVERYTHING
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.domain.flag_map import (
    FLAG_MAP,
    FlagCombination,
    FlagRejection,
    combination,
    resolve_flags,
)
from l3io.wp.backup.domain.journal import (
    JOURNALED_CONSTANTS,
    JournalHeader,
    JournalRecord,
)
from l3io.wp.backup.domain.preflight import ToolRequirement
from l3io.wp.backup.domain.restore import (
    ConfigPolicy,
    OverrideSet,
    RestoreMode,
    RestorePhase,
    SnapshotPolicy,
    restores_database,
    restores_files,
)
from l3io.wp.backup.domain.search_replace import SearchReplaceReport
from l3io.wp.backup.domain.site import SiteDescriptor
from l3io.wp.backup.domain.storage_target import StorageTarget
from l3io.wp.backup.ports.crypto import PayloadCipher
from l3io.wp.backup.ports.journal import RunJournalCommand, RunJournalQuery
from l3io.wp.backup.ports.lock import SiteLock
from l3io.wp.backup.ports.restore import RestorePorts, restore_ports
from l3io.wp.backup.ports.search_replace import SearchReplaceCommand
from l3io.wp.backup.ports.site_files import SiteFilesQuery
from l3io.wp.backup.ports.tracing import Tracer

__all__ = [
    "NO_SNAPSHOT_FLAG",
    "RESTORE_EXTENSION",
    "ArchiveIdentityUnconfirmedError",
    "DatabaseNotRestoredError",
    "NonEmptyTargetRefusal",
    "RelocationFlags",
    "RestoreExtension",
    "RestoreExtensionFactory",
    "RestoreOutcome",
    "RestoreRequest",
    "check_restore_arguments",
    "completion_line",
    "config_policy_from_cli",
    "database_report_json",
    "database_report_lines",
    "drive_restore",
    "emit_database_report",
    "flag_combination",
    "parse_archive_database",
    "render_flags",
    "require_database_restored",
    "restore_mode_from_cli",
    "restore_summary",
    "run_restore",
    "snapshot_policy_from_cli",
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

#: What opts out of the Safety Snapshot on the command line (BL-E003-050).
NO_SNAPSHOT_FLAG: Final = "--no-safety-snapshot"
#: Who acknowledges an interrupted Run resolved from the command line.
_RESOLVED_BY: Final = "cli"
#: The verified copy of the Archive, and the files the root writes, in the workspace.
_ARCHIVE_DIR: Final = "archive"
_WORKSPACE: Final = "workspace"
_DESCRIPTOR_DIR: Final = "descriptor"
_CONFIG_NAME: Final = "wp-config.php"
_PAYLOAD: Final = "decrypted-payload"
_CONFIG_PAYLOAD: Final = "decrypted-config-payload"
_DIR_MODE: Final = 0o700
_FILE_MODE: Final = 0o600
_STAMP: Final = "%Y%m%dT%H%M%SZ"
_EPIC_7: Final = "Epic 7 (relocation)"
_SCOPE_TO_CLI: Final = {scope: text for text, scope in CLI_MODE_TO_SCOPE.items()}

#: How a database warning code reads in the report; others print as the code.
_WARNING_TEXT = {
    "password_unverified": (
        "the WordPress account's host does not match this machine, so its "
        "password was not verified by logging in and an existing password was "
        "left unchanged"
    ),
}
_PREVIOUS_TEXT = {
    "dropped": "dropped",
    "retained": "retained, not removed",
    "none": "none (no live database existed)",
    "absent": "none (loaded in place)",
}


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
# FR-9: the database report, and the exit rule over it
# --------------------------------------------------------------------------


class DatabaseNotRestoredError(BackupError):
    """A real Run's plan loaded the Dump and the report does not say restored.

    ``FAILED``. Every database failure raises where it happens; this is the
    root's last check that "done" is never printed for a database not loaded.
    """

    def __init__(self, database: str | None) -> None:
        self.database = database
        named = "no database report" if database is None else repr(database)
        super().__init__(f"the database was not restored ({named})")


def _flag(done: bool, yes: str, no: str) -> str:
    return yes if done else no


def database_report_lines(report: DatabaseRestoreReport) -> tuple[str, ...]:
    """The report as human lines; a Dry Run's says "would", never "restored"."""
    after, before = report.live_database_after, report.live_database_before
    if not report.performed:
        would = [
            f"database: would ensure {after!r} (database, WordPress user, password, "
            "grant)",
            f"database: would load the Dump into {after!r}",
            f"database: would switch wp-config.php from {before!r} to {after!r}",
        ]
        if report.previous_database == "dropped":
            would.append(f"database: would drop the previous database {before!r}")
        return (*would, "database: not restored (Dry Run)")
    ensure = report.ensure
    lines = [
        f"database: the site now runs on {after!r} (was {before!r})",
        "database: "
        + _flag(ensure.created_database, "created", "already existed")
        + f" {after!r}",
        "database: WordPress user "
        + _flag(ensure.created_user, "created", "already existed"),
        "database: password " + _flag(ensure.password_set, "set", "unchanged"),
        "database: access " + _flag(ensure.granted, "granted", "already granted"),
        f"database: loaded {report.loaded_bytes} bytes in "
        f"{report.load_duration_ms:.0f} ms",
        f"database: previous database {before!r}: "
        + _PREVIOUS_TEXT[report.previous_database],
    ]
    lines.extend(
        f"database: warning {code}: {_WARNING_TEXT.get(code, code)}"
        for code in ensure.warnings
    )
    lines.append(_flag(report.restored, "database: restored", "database: NOT restored"))
    return tuple(lines)


def database_report_json(report: DatabaseRestoreReport) -> str:
    """The report as one JSON object (``--log-format json``)."""
    return json.dumps({"database_report": database_report_fields(report)})


def emit_database_report(report: DatabaseRestoreReport) -> None:
    """Print the report on stdout in the configured format."""
    if configured_format() == "json":
        emit_result(database_report_json(report))
        return
    emit_result("\n".join(database_report_lines(report)))


class _Phased(Protocol):
    @property
    def phases(self) -> tuple[RestorePhase, ...]: ...


class _Reported(Protocol):
    @property
    def database(self) -> DatabaseRestoreReport | None: ...


def require_database_restored(
    plan: _Phased, outcome: _Reported, *, dry_run: bool
) -> None:
    """FR-9's exit rule; raises :class:`DatabaseNotRestoredError` (``FAILED``).

    A plan with ``LOAD_DUMP`` exits ``OK`` in a real Run only when the report
    says ``restored``; a Dry Run that completed its plan exits ``OK`` with a
    not-performed report (ADR-0022). A plan without ``LOAD_DUMP`` must carry
    no report at all.
    """
    loads = RestorePhase.LOAD_DUMP in plan.phases
    database = outcome.database
    if not loads:
        if database is not None:
            msg = "a plan without LOAD_DUMP returned a database report"
            raise ValueError(msg)
        return
    if dry_run and database is not None and not database.performed:
        return
    if database is None or not database.restored or not database.performed:
        raise DatabaseNotRestoredError(
            None if database is None else database.live_database_after
        )


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
    spelling of ``authorizing``. Names paths and ``host:port/name`` only.
    """

    def __init__(self, original: NonEmptyTargetError) -> None:
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


def _refusal(exc: NonEmptyTargetError) -> NonEmptyTargetRefusal:
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
    """The target has no ``wp-config.php`` and ``--archive-database`` did not
    name the Archive config's database (RT-1).

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
        raise UsageError(msg)
    if ":" in user:
        msg = (
            "--archive-database: the user part holds ':'; give USER@HOST[:PORT]/NAME "
            "with no password"
        )
        raise UsageError(msg)
    return user, database_location_from_db_host(host, name)


def _confirm_identity(stated: str | None, *, site: SiteDescriptor, user: str) -> None:
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
    ``SecretRegistry`` before anything uses it).
    """

    new_site_url: str | None
    new_home_url: str | None
    new_db_host: str | None
    new_db_name: str | None
    new_db_user: str | None
    new_db_password: str | None = field(repr=False)
    wp_config: Path | None

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
    """``adapters/wpcli.py::restore_extension``'s signature."""

    def __call__(
        self, flags: RelocationFlags, *, secrets: SecretRegistry
    ) -> RestoreExtension: ...


#: Epic 7 replaces this one line with its static import (the ownership grant).
RESTORE_EXTENSION: RestoreExtensionFactory | None = None


class _NoExtension:
    """No Epic 7: no relocation, no tools, no relocated account."""

    __slots__ = ()

    def required_tools(self) -> tuple[ToolRequirement, ...]:
        return ()

    def wordpress_spec(self) -> CredentialSpec | None:
        return None

    def search_replace(self) -> SearchReplaceCommand | None:
        return None

    def resolve(
        self,
        *,
        site_root: Path,
        site: SiteDescriptor,
        view: ArchiveView,
        files: SiteFilesQuery,
        mode: RestoreMode,
    ) -> RestoreRelocation:
        del site_root, site, view, files, mode
        return RestoreRelocation.none()


# --------------------------------------------------------------------------
# The request and the outcome
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class RestoreRequest:
    """Everything a restore needs from its caller; :func:`drive_restore` does the rest.

    ``mode``, ``overrides``, ``snapshot`` and ``config_policy`` have no
    defaults (AD-5). ``snapshot_dir=None`` is the per-Site default under the
    per-user state directory. ``archive_database`` is ``--archive-database``'s
    raw ``USER@HOST[:PORT]/NAME`` and ``admin_tls`` the ``--admin-tls`` /
    ``--admin-tls-ca`` override (BL-E002-114). ``opted_out_by`` names what
    chose ``SnapshotPolicy.SKIP`` (BL-E003-050). No field carries an
    ``l3io.wp.database`` type (ADR-0015 amendment).
    """

    site_root: Path
    archive: StorageTarget
    mode: RestoreMode
    overrides: OverrideSet
    snapshot: SnapshotPolicy
    snapshot_dir: Path | None
    config_policy: ConfigPolicy
    resolve_run_id: str | None
    admin: CredentialSpec | None
    cipher: PayloadCipher | None
    archive_database: str | None
    admin_tls: AdminTls
    relocation: RelocationFlags
    dry_run: bool
    extra_tools: tuple[ToolRequirement, ...] = ()
    opted_out_by: str = NO_SNAPSHOT_FLAG

    def __post_init__(self) -> None:
        if not self.opted_out_by:
            msg = "opted_out_by must name what may skip the Safety Snapshot"
            raise ValueError(msg)
        # M-3: ``None`` would bind the backup root's plaintext connections.
        # ``AdminTls(mode=None, ca_file=None)`` is the policy; nothing weaker
        # is reachable by omission.
        if not isinstance(cast("object", self.admin_tls), AdminTls):
            msg = (
                "admin_tls must be an AdminTls; AdminTls(mode=None, ca_file=None) "
                "is tls_required_unless_local"
            )
            raise TypeError(msg)


@dataclass(frozen=True, slots=True, kw_only=True)
class RestoreOutcome:
    """What one restore did (or, in a Dry Run, planned); built, never printed.

    ``resolved_run_id`` is set when the Run only resolved an interrupted
    journal (no phase ran); ``integrity`` is then ``None``.
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
    skipped_links: tuple[SkippedLink, ...]
    wp_config_kept: bool


# --------------------------------------------------------------------------
# drive_restore and its steps
# --------------------------------------------------------------------------


def _canonical(site_root: Path) -> Path:
    try:
        canonical = Path(site_root).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        msg = "site root does not exist"
        raise InvalidSiteDescriptorError(msg, paths=(str(site_root),)) from exc
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


def _bind_archive(archive: StorageTarget, workspace: Path) -> StorageTarget:
    """RT-001: the Archive copied into the workspace; every later read is of it."""
    source = Path(archive.path)
    destination = _private_dir(workspace / _ARCHIVE_DIR) / (source.name or "archive")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        reader = source.open("rb")
    except OSError as exc:
        raise ArchiveNotFoundError(source) from exc
    try:
        # ADV-S02-02: refuse before the copy what the workspace cannot hold.
        need = os.fstat(reader.fileno()).st_size
        free = shutil.disk_usage(destination.parent).free
        if need > free * (1 - FREE_SPACE_MARGIN):
            reader.close()
            raise InsufficientSpaceError(_WORKSPACE, needed=need, free=free)
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

    def __init__(self, archive: StorageTarget, workspace: Path) -> None:
        self._archive = archive
        self._workspace = workspace
        self._copy: StorageTarget | None = None
        self._view: ArchiveView | None = None

    @property
    def copy(self) -> StorageTarget:
        if self._copy is None:
            self._copy = _bind_archive(self._archive, self._workspace)
        return self._copy

    def view(self) -> ArchiveView:
        if self._view is None:
            self._view = classify(TarArchiveProbe().member_index(self.copy))
        return self._view


def _require_cipher(view: ArchiveView, cipher: PayloadCipher | None) -> None:
    if view.encrypted and cipher is None:
        operation: Final = "restore"
        raise PassphraseRequiredError(operation)


def _archive_config(
    archive: _Archive, *, workspace: Path, cipher: PayloadCipher | None
) -> Path:
    """The Archive's config member, read bounded into a ``0600`` workspace file."""
    view = archive.view()
    _require_cipher(view, cipher)
    member = config_member(view)
    if member is None:
        msg = "neither the target nor the archive has a wp-config.php"
        raise InvalidSiteDescriptorError(msg, paths=(str(archive.copy.path),))
    payload: Path | None = None
    if view.encrypted and cipher is not None:
        # Decrypted here so a wrong passphrase stops the Run before anything
        # changes; VERIFY decrypts again into its own file (ADR-0024).
        payload = decrypt_payload(
            archive.copy, view, cipher=cipher, destination=workspace / _CONFIG_PAYLOAD
        )
    try:
        body = TarArchiveProbe().read_member(
            archive.copy, view, member.path, max_bytes=CONFIG_MAX_BYTES, payload=payload
        )
    finally:
        if payload is not None:
            payload.unlink(missing_ok=True)
    directory = _private_dir(workspace / _DESCRIPTOR_DIR)
    return _write_private(directory / _CONFIG_NAME, body)


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
            msg = (
                f"no interrupted run {resolve_run_id} is recorded for this Site; "
                "nothing was resolved"
            )
            raise RefusedError(msg)
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
    *,
    performs_effects: bool,
) -> RestorePorts:
    """The one call of ``restore_ports``; preflight exactly as ``cli/backup.py``."""
    return restore_ports(
        run.adapters,
        preflight=functools.partial(
            require_tools,
            DumpReadinessPreflight(
                run.adapters.database_query,
                site.database_location(),
                credential_source=site.credential_source,
            ),
        ),
        free_bytes=lambda path: shutil.disk_usage(path).free,
        cutover_credentials=cutover,
        performs_effects=performs_effects,
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

    In every Mode that restores the database the live database is surveyed
    by the administrator, so ``TargetState.database`` is ``None`` only for a
    Mode that does not touch it: "not looked at" is never read as "empty".
    """
    location = site.database_location()

    def database(at: DatabaseLocation | None) -> DatabaseTargetState | None:
        if at is None or not restores_database(mode):
            return None
        return run.adapters.database_query.survey_target(
            at, table_prefix=site.table_prefix, wordpress_user=wordpress_user or None
        )

    def survey() -> TargetState:
        files = run.adapters.site_files_query
        live = database(location)
        if restores_database(mode) and live is None:  # pragma: no cover - by type
            msg = "a database Mode was planned without surveying the live database"
            raise ValueError(msg)
        return TargetState(
            site_root_empty=files.is_empty(site_root),
            database=live,
            config_exists=files.file_exists(target_config),
            site_root=PurePosixPath(site_root),
            site_root_entries=files.entry_count(site_root),
            database_location=location if live is not None else None,
            load_target=database(relocation.load_location),
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
            phase=action.phase.value,
            action=action.text,
            fields=dict(action.fields),
        )


def _prior_constants(run: RunContext, config_file: Path) -> dict[str, str | None]:
    keys = sorted(JOURNALED_CONSTANTS)
    read = run.adapters.config_query.read_constants(config_file, keys)
    return {k: None if read.get(k) is None else str(read.get(k)) for k in keys}


def drive_restore(
    request: RestoreRequest,
    *,
    ctx: RunContext | None,
    tracer: Tracer | None = None,
    secrets: SecretRegistry | None = None,
    command_args: Sequence[str] | None = None,
    site_lock: SiteLock | None = None,
) -> RestoreOutcome:
    """The restore driver: returns a :class:`RestoreOutcome` or raises; never prints.

    Steps, each before the next, nothing destructive before the executor:
    the Site Lock and the Run (1); the journal query (2); the Site Descriptor,
    the credential sources and the one bind (3), with the Archive identity
    confirmed on the no-config path before any database call; resolution of
    an interrupted Run, which stops the Run (4); the probe (5); the plan,
    FR-7's guard included (6); the header and inputs (7); ``execute_restore``
    (8); the outcome (9). An error escaping after ``SNAPSHOT`` completed
    carries ``snapshot_path``.
    """
    if request.config_policy is ConfigPolicy.OVERWRITE and not restores_files(
        request.mode
    ):
        msg = (
            "--overwrite-wp-config needs a mode that restores files; "
            f"Restore Mode {request.mode.value} restores none"
        )
        raise UsageError(msg)
    factory = RESTORE_EXTENSION
    if factory is None and not request.relocation.is_empty:
        named = ", ".join(request.relocation.given)
        msg = f"relocation ({named}) is not available in this build: {_EPIC_7}"
        raise UsageError(msg)
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
        if request.relocation.new_db_password:
            run.secrets.register(
                request.relocation.new_db_password, source="--new-db-password"
            )
        _log.info(
            "restore_mode_resolved",
            restore_mode=request.mode.value,
            overrides=sorted(o.value for o in request.overrides.waived),
            snapshot_policy=request.snapshot.value,
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
        ext: RestoreExtension = (
            _NoExtension()
            if factory is None
            else factory(request.relocation, secrets=run.secrets)
        )
        # Step 3: the Site Descriptor, the credential sources, the one bind.
        archive = _Archive(request.archive, run.workspace)
        located = _locate(site_root, request.relocation.wp_config)
        if located is not None:
            site, wp_config = site_descriptor_from_wp_config(
                site_root, config_file=located.path
            )
            target_config, config_dir = located.path, located.path.parent
            descriptor_file = located.path
        else:
            descriptor_file = _archive_config(
                archive, workspace=run.workspace, cipher=request.cipher
            )
            site, wp_config = site_descriptor_from_archive_config(
                site_root, config_file=descriptor_file
            )
            located = LocatedConfig(path=descriptor_file, location=None)
            target_config, config_dir = site_root / _CONFIG_NAME, site_root
        run.run_span.set_attributes(db_name=site.db_name)
        # BL-E002-114: an override below the host's TLS policy is USAGE here,
        # before any connection.
        check_admin_tls(request.admin_tls, site.db_host)
        admin = admin_source(request.admin, environ=os.environ, secrets=run.secrets)
        wordpress = wordpress_source(
            None, wp_config=wp_config, environ=os.environ, secrets=run.secrets
        )
        relocated_spec = ext.wordpress_spec()
        relocated = (
            None
            if relocated_spec is None
            else wordpress_source(
                relocated_spec,
                wp_config=wp_config,
                environ=os.environ,
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
                admin_tls=request.admin_tls,
                create_archive_directory=True,
            )
            run.bind_adapters(dry_run_adapters(real) if request.dry_run else real)
        db_user = _db_user(run, descriptor_file)
        if located.location is None and descriptor_file != target_config:
            _confirm_identity(request.archive_database, site=site, user=db_user)
        elif request.archive_database is not None:
            _log.info("archive_database_unused", reason="the target has wp-config.php")
        cutover = None if relocated is None else password_at_call_time(relocated)
        ports = _ports(run, site, cutover, performs_effects=not request.dry_run)
        # Step 4: resolution only — resolving and restoring are never one command.
        if resolving is not None:
            record = resolving
            complete_cutover(
                record,
                ports=ports,
                table_prefix=site.table_prefix,
                journal_path=str(journal_directory(site_root) / CURRENT),
            )
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
            )
        # Step 5: the probe (reused when step 3 already probed).
        view = archive.view()
        if not view.encrypted:
            # RT-002 waiver (ADR-0016 amendment, 2026-11-06): restore, warning
            # before any effect, so the warning is seen even if the Run fails.
            _log.warning(
                "archive_unauthenticated",
                integrity=view.integrity.value,
                archive=str(request.archive.path),
            )
            print(
                STRUCTURAL_ONLY_WARNING
                if view.integrity is Integrity.STRUCTURAL_ONLY
                else UNAUTHENTICATED_WARNING,
                file=sys.stderr,
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
            raise _refusal(exc) from exc
        _log_plan(plan, target)
        # Step 7: the journal header and the executor's inputs.
        stamp = run.started_at.astimezone(UTC).strftime(_STAMP)
        snapshot_path = snapshot_dir / f"{stamp}-{run.run_id}.tar.gz"
        saved_config = (
            str(snapshot_dir / f"{stamp}-{run.run_id}-wp-config.php")
            if request.config_policy is ConfigPolicy.OVERWRITE
            else None
        )
        load, staging = load_names(site.db_name, run.run_id, relocation)
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
            staging_database=staging,
            wp_config_path=str(target_config),
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
                    bytes=result.bytes,
                    performed=result.performed,
                )
            )
            if request.cipher is None and taken[-1].performed:
                print(
                    f"safety snapshot is NOT encrypted: {result.path}", file=sys.stderr
                )
                _log.warning("safety_snapshot_unencrypted", path=str(result.path))
            return taken[-1]

        # Step 8: the executor runs every phase.
        try:
            report = execute_restore(
                plan,
                inputs,
                ports,
                take_snapshot,
                functools.partial(check_staged, ports=ports),
                functools.partial(load_staged, ports=ports, workspace=run.workspace),
                run.adapters.journal_command,
                request.cipher,
                survey_target=survey,
                opted_out_by=request.opted_out_by,
            )
        except NonEmptyTargetError as exc:
            refusal = _refusal(exc)
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
            skipped_links=report.skipped_links,
            wp_config_kept=report.wp_config_kept,
        )


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


# --------------------------------------------------------------------------
# run_restore — the CLI root
# --------------------------------------------------------------------------


def _status(outcome: RestoreOutcome, *, completed: bool) -> str:
    if not completed:
        return "failed"
    if outcome.resolved_run_id is not None:
        return "would_resolve" if outcome.dry_run else "resolved"
    return "planned" if outcome.dry_run else "completed"


def restore_summary(
    outcome: RestoreOutcome, *, archive: str, site_root: str, completed: bool
) -> dict[str, object]:
    """UX-S02-04: everything a Run prints, as the one ``--log-format json`` object.

    ``status`` is ``completed`` (a real Run), ``planned`` (a Dry Run),
    ``resolved``/``would_resolve`` (``--resolve-interrupted-run``) or
    ``failed`` (the report does not call the database restored); ``actions``
    is the Dry Run's ordered plan, empty for a real Run.
    """
    database = outcome.database
    search = outcome.search_replace
    return {
        "status": _status(outcome, completed=completed),
        "run_id": outcome.run_id,
        "dry_run": outcome.dry_run,
        "restore_mode": outcome.mode.value,
        "archive": archive,
        "site_root": site_root,
        "resolved_run_id": outcome.resolved_run_id,
        "actions": [a.text for a in outcome.actions] if outcome.dry_run else [],
        "skipped_links": [
            {"path": str(link.path), "link_target": str(link.link_target)}
            for link in outcome.skipped_links
        ],
        "database_report": (
            None if database is None else database_report_fields(database)
        ),
        "search_replace": (
            None if search is None else {"replacements": search.replacements}
        ),
        "snapshot_path": (
            None if outcome.snapshot_path is None else str(outcome.snapshot_path)
        ),
        "snapshot_encrypted": outcome.snapshot_encrypted,
        "wp_config_kept": outcome.wp_config_kept,
    }


def completion_line(outcome: RestoreOutcome, *, archive: str, site_root: str) -> str:
    """UX-S02-04: the last line of a successful text-mode Run."""
    what = f"Restore Mode {outcome.mode.value}, archive {archive}, site {site_root}"
    if outcome.dry_run:
        return f"Dry Run complete ({what}); nothing was changed"
    return f"restore complete ({what})"


def _print_outcome(
    outcome: RestoreOutcome, *, archive: str, site_root: str, completed: bool
) -> None:
    """Exactly what ``outcome`` carries, on stdout (stderr for warnings).

    Under ``--log-format json`` stdout is one JSON object,
    ``{"restore": restore_summary(...)}``, and nothing else (UX-S02-04).
    Otherwise the human lines, ending — when ``completed`` — with
    :func:`completion_line`, so a Run that changed nothing worth reporting
    still says it finished.
    """
    for link in outcome.skipped_links:
        print(
            f"WARNING: symlink {link.path} -> {link.link_target} not restored: "
            "absolute and out-of-root symlinks are not supported",
            file=sys.stderr,
        )
    if configured_format() == "json":
        summary = restore_summary(
            outcome, archive=archive, site_root=site_root, completed=completed
        )
        emit_result(json.dumps({"restore": summary}))
        return
    if outcome.resolved_run_id is not None:
        verb = "would resolve" if outcome.dry_run else "resolved"
        emit_result(f"{verb} {outcome.resolved_run_id}")
        return
    if outcome.dry_run:
        emit_result("\n".join(action.text for action in outcome.actions))
    if outcome.database is not None:
        emit_database_report(outcome.database)
    if outcome.search_replace is not None:
        emit_result(f"search-replace: {outcome.search_replace.replacements} replaced")
    if outcome.snapshot_path is not None:
        emit_result(f"safety snapshot: {outcome.snapshot_path}")
    if outcome.wp_config_kept:
        emit_result(
            "wp-config.php kept (target's); pass --overwrite-wp-config to replace it"
        )
    if completed:
        emit_result(completion_line(outcome, archive=archive, site_root=site_root))


def _print_kept_snapshot(path: PurePath, *, error: BaseException) -> None:
    """A failed Run's Safety Snapshot, on stdout in the configured format."""
    if configured_format() == "json":
        failed = {
            "status": "failed",
            "error": type(error).__name__,
            "snapshot_path": str(path),
        }
        emit_result(json.dumps({"restore": failed}))
        return
    emit_result(f"safety snapshot: {path}")


def run_restore(args: argparse.Namespace, *, secrets: SecretRegistry) -> ExitCode:
    """``wpbackup2 restore``: ``ExitCode.OK``, or raises ``BackupError``.

    Argument translation only, then one :func:`drive_restore` call. The
    passphrase is resolved into ``secrets`` on every restore (AD-26) so the
    one passphrase decrypts an encrypted Archive and encrypts the Safety
    Snapshot; with none, the snapshot is written unencrypted and said so.
    """
    # Passthrough: copied verbatim, validated by Epic 7.
    relocation = RelocationFlags(
        new_site_url=args.new_site_url,
        new_home_url=args.new_site_home_url,
        new_db_host=args.new_db_host,
        new_db_name=args.new_db_name,
        new_db_user=args.new_db_user,
        new_db_password=args.new_db_password,
        wp_config=None if args.wp_config is None else Path(args.wp_config),
    )
    if relocation.new_db_password:
        secrets.register(relocation.new_db_password, source="--new-db-password")
    mode, overrides = restore_mode_from_cli(
        mode=args.mode,
        cleanfirst=args.cleanfirst,
        drop_database=args.drop_database,
        force=args.force,
    )
    resolved = resolve_passphrase(
        explicit=None, environ=os.environ, prompt=terminal_prompt(), secrets=secrets
    )
    request = RestoreRequest(
        site_root=Path(args.wp_dir),
        archive=storage_target_from_cli(args.archive),
        mode=mode,
        overrides=overrides,
        snapshot=snapshot_policy_from_cli(no_safety_snapshot=args.no_safety_snapshot),
        snapshot_dir=None if args.snapshot_dir is None else Path(args.snapshot_dir),
        config_policy=config_policy_from_cli(
            overwrite_wp_config=args.overwrite_wp_config
        ),
        resolve_run_id=args.resolve_interrupted_run,
        admin=admin_spec(
            username=args.admin_username,
            password=args.admin_password,
            aws_secret_id=args.admin_credentials_aws_secret_id,
            aws_region=args.admin_credentials_aws_region,
            environ=os.environ,
        ),
        cipher=None if resolved is None else PassphraseCipher(resolved.value),
        archive_database=args.archive_database,
        admin_tls=AdminTls(
            mode=args.admin_tls,
            ca_file=None if args.admin_tls_ca is None else Path(args.admin_tls_ca),
        ),
        relocation=relocation,
        dry_run=bool(args.what_if),
    )
    try:
        outcome = drive_restore(request, ctx=None, secrets=secrets)
    except BaseException as exc:
        kept = _kept_snapshot(exc)
        if kept is not None:
            _print_kept_snapshot(kept, error=exc)
        raise
    shown = {"archive": str(request.archive.path), "site_root": str(request.site_root)}
    try:
        require_database_restored(outcome, outcome, dry_run=outcome.dry_run)
    except BackupError:
        _print_outcome(outcome, completed=False, **shown)
        raise
    _print_outcome(outcome, completed=True, **shown)
    return ExitCode.OK
