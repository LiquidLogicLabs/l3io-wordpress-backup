"""AD-14 — the package exception hierarchy.

One base, :class:`BackupError`, carrying its structured fields as attributes.
Every exception from ``l3io.wp.database``, ``tarfile``, ``OSError`` or ``json``
that reaches a public boundary is re-raised as one of these with
``raise ... from``. ``tests/contracts/test_exception_hierarchy.py`` walks the
package and asserts every exception class defined in it derives from
:class:`BackupError`.

``str()`` of an error is a message built from its fields and is **not**
redacted (ADR-0018): redaction is applied where text leaves the program. No
field ever carries a credential value — only labels, keys, paths and codes.

``snapshot_path`` is typed :class:`~pathlib.PurePath` because ``domain/`` may
not import the I/O-capable ``Path`` (AD-1); a ``Path`` is a ``PurePath``, so
the restore driver sets it with the real path.

**Exit codes (FR-21).** Every class carries its process exit code as the class
attribute ``exit_code``, a member of
:class:`~l3io.wp.backup.domain.exit_codes.ExitCode`. :class:`BackupError` says
``FAILED``; exactly the classes that name a different kind override it, and
every subclass inherits from its base. A later class inherits its code or
declares ``exit_code`` itself with an **existing** member — nothing in ``cli/``
changes. :func:`exit_code_of` reads the attribute for any exception.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import PurePath
from typing import TYPE_CHECKING, ClassVar, Final, Literal

from l3io.wp.backup.domain.exit_codes import ExitCode

if TYPE_CHECKING:
    from l3io.wp.backup.domain.archive_format import VerificationResult

__all__ = [
    "AdaptersAlreadyBoundError",
    "AdaptersNotBoundError",
    "AdminCredentialsRequiredError",
    "ArchiveDirectoryNotWritableError",
    "ArchiveExistsError",
    "ArchiveHasNoDumpError",
    "ArchiveNotEncryptedError",
    "ArchiveNotFoundError",
    "ArchiveReadError",
    "ArchiveWriteError",
    "BackupError",
    "CleanFailedError",
    "ConfigLocationConflictError",
    "ConfigWriteFailedError",
    "CredentialsError",
    "DatabaseRefusedError",
    "DatabaseUnreachableError",
    "DumpFailedError",
    "EnvironmentDrivenTargetError",
    "ExtractionRefusedError",
    "ExtractionWriteError",
    "FlagCombinationRejectedError",
    "IncompleteInstallError",
    "IncompleteRelocationCredentialsError",
    "InstallationNotReadableError",
    "InterferingReplacementsError",
    "InvalidRelocationValueError",
    "InvalidSiteDescriptorError",
    "LoadFailedError",
    "LoadReadinessError",
    "MalformedManifestError",
    "ManifestTooLargeError",
    "MemberReadRefusedError",
    "MissingExtraError",
    "NoArchiveForSiteError",
    "PassphraseMismatchError",
    "PassphraseRejectedError",
    "PassphraseRequiredError",
    "PassphraseTooShortError",
    "PassphraseUnavailableError",
    "PathNotAccessibleError",
    "PayloadDamagedError",
    "PreflightFailedError",
    "PrivilegeDropError",
    "ProvisioningFailedError",
    "PruneFailedError",
    "RefusedError",
    "RelocationNeedsDatabaseRestoreError",
    "RootBackupRefusedError",
    "RootRestoreRefusedError",
    "RunContextActiveError",
    "RunTerminated",
    "SearchReplaceIncompleteError",
    "SiteLockLocationError",
    "SiteLockReleaseError",
    "SiteLockedError",
    "SiteRootNotFoundError",
    "StoredUrlInvalidError",
    "StoredUrlMissingError",
    "TargetNotWritableError",
    "UnboundAdapterError",
    "UndefinedConfigFunctionError",
    "UnknownInterruptedRunError",
    "UnreadableSiteRootError",
    "UnrecognisedArchiveError",
    "UnsafeCleanTargetError",
    "UnsafeMemberPathError",
    "UnsupportedConfigLocationError",
    "UnsupportedConfigValueError",
    "UnsupportedFormatVersionError",
    "UnsupportedStorageSchemeError",
    "UsageError",
    "VerificationFailedError",
    "WorkFactorTooHighError",
    "WpCliConfigFoundError",
    "WpCliFailedError",
    "WpCliOutputUnrecognisedError",
    "exit_code_of",
]


class BackupError(Exception):
    """The base of every error this package raises across a public boundary.

    Attributes every subclass has: ``exit_code`` (class attribute, an
    :class:`~l3io.wp.backup.domain.exit_codes.ExitCode`) and ``snapshot_path``
    (the Safety Snapshot's path, or ``None``). Each concrete subclass adds
    typed, named fields carrying what its message states; its docstring names
    them. No field ever holds a credential, passphrase or secret value.
    """

    #: The process exit code this kind of failure ends a Run with.
    exit_code: ClassVar[ExitCode] = ExitCode.FAILED

    #: Set by the restore driver on any error escaping after the Safety
    #: Snapshot completed, so the failure output names it.
    snapshot_path: PurePath | None = None

    def __reduce__(self) -> tuple[Callable[..., BackupError], tuple[object, ...]]:
        """Pickle (and ``copy``) by state, not by calling ``__init__`` again.

        ``BaseException`` rebuilds with ``cls(*self.args)``, and ``args`` is
        the one formatted message; a class whose constructor takes fields
        (``DumpFailedError(..., database=...)``) cannot be rebuilt from that.
        A worker pool or task queue would lose the error and surface a
        ``TypeError`` or ``BrokenProcessPool`` instead. ``__cause__`` and the
        traceback do not travel, as with any exception.
        """
        return (_rebuild_error, (type(self), self.args, dict(self.__dict__)))


def _rebuild_error(
    cls: type[BackupError], message: tuple[object, ...], state: dict[str, object]
) -> BackupError:
    """The inverse of :meth:`BackupError.__reduce__`: no ``__init__`` call."""
    error = cls.__new__(cls)
    Exception.__init__(error, *message)
    error.__dict__.update(state)
    return error


class RefusedError(BackupError):
    """A check found a reason not to act, so nothing was done.

    A grouping base: it is raised only as one of its subclasses, each of
    which carries its own fields.
    """

    exit_code: ClassVar[ExitCode] = ExitCode.REFUSED


class UsageError(BackupError):
    """The caller supplied an invalid combination of arguments.

    ``arguments`` names the arguments at fault, never their values, in one
    vocabulary (BL-E006-015): on every path the library reaches, the library
    parameter's name (``db_tls``, ``config_policy``), dotted for a field of
    one (``db_tls.ca_file``, ``relocation.new_site_url``); an environment
    variable by its name (``PATH``). Only the command line's own checks,
    which no library call reaches, name its flags (``--admin-password``). It
    is empty where no single argument is at fault: a combination, or an
    internal invariant that is a defect rather than a caller's mistake. The
    message text may name the flag as well; ``arguments`` is the structured
    field.
    """

    exit_code: ClassVar[ExitCode] = ExitCode.USAGE

    def __init__(self, message: str, *, arguments: tuple[str, ...]) -> None:
        self.arguments = arguments
        super().__init__(message)


class FlagCombinationRejectedError(UsageError):
    """AD-25 — restore flags that express no Restore Mode.

    The message is the flag table's own entry: why the combination was
    rejected (``reason``) and the combination that would express a real Mode
    (``use_instead``). ``arguments`` is empty: the combination is at fault,
    not one flag.
    """

    def __init__(self, *, reason: str, use_instead: str) -> None:
        self.reason = reason
        self.use_instead = use_instead
        super().__init__(f"{reason}; {use_instead}", arguments=())


class PassphraseUnavailableError(UsageError):
    """AD-26 — passphrase encryption was requested and no usable one resolved.

    ``variable`` is the environment variable consulted, never its value;
    ``reason`` says why none was usable; ``argument`` is the root's own name
    for a passphrase its caller passes (the library's ``passphrase``), named
    in the hint; ``None`` (the CLI, which takes no passphrase value) leaves it
    out (UX-01-17). ``arguments`` is ``("passphrase",)``.
    """

    def __init__(
        self, variable: str, *, reason: str, argument: str | None = None
    ) -> None:
        self.variable = variable
        self.reason = reason
        self.argument = argument
        passed = "" if argument is None else f"pass {argument}=, "
        super().__init__(
            f"no usable passphrase: {reason} ({passed}set {variable}, "
            "or run on a terminal so it can be prompted for)",
            arguments=("passphrase",),
        )


class PassphraseTooShortError(UsageError):
    """ADR-0024 amendment (BL-E004-052): an encryption passphrase below the floor.

    Raised before any effect when a passphrase that would **encrypt** (a
    backup, the library ``backup``) is shorter than ``minimum`` code points.
    Decryption accepts any passphrase. Never carries the value.
    """

    def __init__(self, *, minimum: int) -> None:
        self.minimum = minimum
        super().__init__(
            f"passphrase refused: an encryption passphrase must be at least "
            f"{minimum} characters",
            arguments=("passphrase",),
        )


class PassphraseMismatchError(UsageError):
    """ADR-0024 amendment: the prompted encryption passphrase was not confirmed.

    The two entries differed; ``prompts`` is how many were asked (2). Never
    carries either value.
    """

    def __init__(self, *, prompts: int = 2) -> None:
        self.prompts = prompts
        super().__init__(
            "passphrase refused: the two passphrases entered did not match",
            arguments=("passphrase",),
        )


class InvalidRelocationValueError(UsageError):
    """FR-11 — a relocation flag's value breaks a rule.

    ``flag`` and ``rule`` name it; the value is never echoed. ``arguments``
    is ``(argument,)``, the parameter name (``relocation.new_site_url``),
    ``flag`` when none is given.
    """

    def __init__(self, flag: str, rule: str, *, argument: str | None = None) -> None:
        self.flag = flag
        self.rule = rule
        self.argument = argument or flag
        super().__init__(f"{flag} {rule}", arguments=(self.argument,))


class IncompleteRelocationCredentialsError(UsageError):
    """FR-11 — half of the relocated account was given.

    Keeping the other half from the Archive would mix two accounts. ``given``
    and ``missing`` name the two flags; ``arguments`` names the two parameters
    (``arguments``, else ``(given, missing)``).
    """

    def __init__(
        self, *, given: str, missing: str, arguments: tuple[str, ...] | None = None
    ) -> None:
        self.given = given
        self.missing = missing
        super().__init__(
            f"{given} needs {missing}: the relocated account is a user and "
            "password pair, and keeping one half from the Archive would mix "
            "two accounts",
            arguments=(given, missing) if arguments is None else arguments,
        )


class RelocationNeedsDatabaseRestoreError(UsageError):
    """FR-11 — relocation flags with a Restore Mode that restores no database.

    Such a Mode has no cutover, so every ``--new-*`` value would be accepted
    and discarded. ``flags`` names them, ``mode`` is the Restore Mode's CLI
    spelling, and ``arguments`` the parameters (``arguments``, else ``flags``).
    """

    def __init__(
        self,
        *,
        flags: tuple[str, ...],
        mode: str,
        arguments: tuple[str, ...] | None = None,
    ) -> None:
        self.flags = flags
        self.mode = mode
        super().__init__(
            f"{', '.join(flags)} need a Restore Mode that restores the database; "
            f"Restore Mode {mode} restores none",
            arguments=flags if arguments is None else arguments,
        )


class InvalidSiteDescriptorError(BackupError):
    """The Site Descriptor could not be built from what was given."""

    def __init__(self, reason: str, *, paths: tuple[str, ...] = ()) -> None:
        self.reason = reason
        self.paths = paths
        where = f" ({', '.join(paths)})" if paths else ""
        super().__init__(f"invalid site: {reason}{where}")


class SiteRootNotFoundError(InvalidSiteDescriptorError):
    """The ``--wp-dir`` (``site``/``site_root``) the caller named does not exist.

    ``USAGE`` (BL-E002-074, owner decision 2026-11-07). ``path`` is the
    absolute path as given.
    """

    exit_code: ClassVar[ExitCode] = ExitCode.USAGE

    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__("site root does not exist", paths=(path,))


class UnsafeMemberPathError(BackupError):
    """An Archive member path would escape its role prefix."""

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"unsafe archive member path {path!r}: {reason}")


class UnsupportedStorageSchemeError(RefusedError):
    """A Storage Target names a scheme this build does not support."""

    def __init__(self, scheme: str) -> None:
        self.scheme = scheme
        super().__init__(f"unsupported storage scheme {scheme!r}; only 'file' is")


class CredentialsError(BackupError):
    """Database credentials could not be obtained or were rejected.

    ``source`` is the provider's label and ``missing_key`` the key that was not
    defined; neither is ever a secret value.
    """

    exit_code: ClassVar[ExitCode] = ExitCode.CREDENTIALS

    def __init__(
        self, source: str, *, missing_key: str | None = None, reason: str = ""
    ) -> None:
        self.source = source
        self.missing_key = missing_key
        self.reason = reason
        detail = f": {missing_key} is not defined" if missing_key else ""
        tail = f" ({reason})" if reason else ""
        super().__init__(f"credentials from {source!r} unusable{detail}{tail}")


class AdminCredentialsRequiredError(CredentialsError):
    """The operation needs administrator credentials that were not given.

    Owner decision 55: a restore needs an administrator only when the
    database does not exist yet or the WordPress account lacks the rights.
    ``missing`` then names what is missing, as finding codes
    (``database_absent``, ``missing_privilege``, ``authentication_failed``,
    ``database_not_visible``), ``database`` the database and ``account`` the
    WordPress account's credential source; never a secret.
    """

    def __init__(
        self,
        source: str,
        *,
        flags: tuple[str, ...],
        missing: tuple[str, ...] = (),
        database: str | None = None,
        account: str | None = None,
    ) -> None:
        self.flags = flags
        self.missing = missing
        self.database = database
        self.account = account
        need = ""
        if missing:
            on = "" if database is None else f" {database!r}"
            who = "" if account is None else f" (credentials from {account!r})"
            need = (
                f"the WordPress account{who} cannot restore database{on} as it "
                f"stands ({', '.join(missing)}); an administrator is needed only "
                "to create the database or the account, or to grant its rights; "
            )
        super().__init__(
            source,
            reason=f"{need}administrator credentials required: {', '.join(flags)}",
        )


class ManifestTooLargeError(RefusedError):
    """BL-E004-053: the Manifest this backup would write exceeds the cap.

    Raised after the Site Root is walked and before anything is written, so
    no Archive appears. ``members`` is the member count, ``size`` the
    Manifest's size in bytes (an upper bound) and ``limit`` the cap.
    """

    def __init__(self, *, members: int, size: int, limit: int) -> None:
        self.members = members
        self.size = size
        self.limit = limit
        super().__init__(
            f"refused: the Manifest for {members} members would be {size} bytes, "
            f"over the {limit}-byte limit an Archive can be read with; exclude "
            "paths (--exclude) or back the Site up in parts"
        )


class DatabaseUnreachableError(BackupError):
    """The database server could not be reached."""

    exit_code: ClassVar[ExitCode] = ExitCode.UNREACHABLE

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        super().__init__(f"database server {host}:{port} could not be reached")


class DumpFailedError(BackupError):
    """The database dump started and failed.

    ``database`` is the database's name; ``exit_status`` the dump's exit
    status when it exited, else ``None``; ``reason`` is structured (that exit
    status or a failure class), never the dependency's text.
    """

    def __init__(
        self, reason: str, *, database: str, exit_status: int | None = None
    ) -> None:
        self.reason = reason
        self.database = database
        self.exit_status = exit_status
        super().__init__(f"database dump failed: {reason}")


class ArchiveWriteError(BackupError):
    """The Archive could not be written (I/O failure, ``ENOSPC`` included)."""

    def __init__(self, path: PurePath, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"could not write archive {str(path)!r}: {reason}")


class ExtractionWriteError(ArchiveWriteError):
    """Extracting the Archive could not write into its destination.

    ``path`` is the directory extracted into, ``reason`` the operating
    system's reason, ``destination`` what that directory is (``the Site
    Root``, or the directory a laid-down ``wp-config.php`` goes into). An
    :class:`ArchiveWriteError`, so the exit code is unchanged; only the
    message names the right thing.
    """

    def __init__(self, path: PurePath, reason: str, *, destination: str) -> None:
        self.path = path
        self.reason = reason
        self.destination = destination
        BackupError.__init__(
            self, f"could not write into {destination} {str(path)!r}: {reason}"
        )


class ArchiveExistsError(RefusedError):
    """The Archive's final name already exists at its destination."""

    def __init__(self, path: PurePath) -> None:
        self.path = path
        super().__init__(f"archive {str(path)!r} already exists")


class MissingExtraError(BackupError):
    """An optional feature was used without its extra installed (AD-8).

    Only for an extra that is still optional (``aws``, ``otel``): what the
    ``database`` extra brought is a required dependency since 1.0.0 (owner
    decision 76), and its absence is :class:`IncompleteInstallError`.
    """

    exit_code: ClassVar[ExitCode] = ExitCode.NOT_READY

    def __init__(self, extra: str) -> None:
        self.extra = extra
        super().__init__(
            f"this feature needs the {extra!r} extra: "
            f"pip install 'l3io-wordpress-backup[{extra}]'"
        )


class IncompleteInstallError(BackupError):
    """A required dependency cannot be imported: the installation is broken.

    The family (``l3io-wordpress-database``, ``l3io-wordpress-config``, the
    ``l3io-age-passphrase`` binder) is a required dependency since 1.0.0
    (owner decision 76), so a pip install always brings it; one made with
    ``--no-deps``, or an environment pruned afterwards, can still lack it.
    ``distribution`` names what is missing. No extra would fix it.
    """

    exit_code: ClassVar[ExitCode] = ExitCode.NOT_READY

    def __init__(self, distribution: str) -> None:
        self.distribution = distribution
        super().__init__(
            f"the installation is incomplete: {distribution!r} is missing; "
            "reinstall l3io-wordpress-backup"
        )


class DatabaseRefusedError(RefusedError):
    """A database check found blockers; ``findings`` are codes only.

    ``database`` is the database the check was about and ``account`` the
    account it answered for (a user name or a credential source's label),
    each ``None`` where the caller does not know it; never a secret.
    """

    def __init__(
        self,
        findings: tuple[str, ...],
        *,
        database: str | None = None,
        account: str | None = None,
    ) -> None:
        self.findings = findings
        self.database = database
        self.account = account
        on = "" if database is None else f" on database {database!r}"
        acting = "" if account is None else f" for account {account!r}"
        super().__init__(
            f"database operation refused{on}{acting}: {', '.join(findings)}"
        )


class PassphraseRequiredError(RefusedError):
    """Encryption was requested, or the Archive is encrypted, and no passphrase.

    ``CREDENTIALS`` (BL-E002-069, owner decision 2026-11-07): a missing key is
    a credentials failure, as ``domain/exit_codes.py`` defines.
    """

    exit_code: ClassVar[ExitCode] = ExitCode.CREDENTIALS

    def __init__(self, operation: Literal["backup", "restore", "verify"]) -> None:
        self.operation = operation
        super().__init__(f"{operation} needs a passphrase and none was resolved")


#: Database's ``ensure`` step names, as the report says them (FR-9).
_PROVISIONING_STEP_TEXT: dict[str, str] = {
    "create_database": "created database",
    "create_user": "created user",
    "set_password": "password set",
    "grant": "granted access",
}


class ProvisioningFailedError(BackupError):
    """Provisioning started and failed part-way.

    ``completed`` is database's list of the steps that ran before the
    failure; the message names each, then the failure, e.g. "created
    database; then provisioning failed".
    """

    def __init__(self, reason: str, *, completed: tuple[str, ...] = ()) -> None:
        self.reason = reason
        self.completed = completed
        done = "; ".join(_PROVISIONING_STEP_TEXT.get(s, s) for s in completed)
        progress = f" ({done}; then provisioning failed)" if done else ""
        super().__init__(f"provisioning failed: {reason}{progress}")


class ConfigWriteFailedError(BackupError):
    """A ``wp-config.php`` write failed. ``written`` names keys, never values."""

    def __init__(
        self, path: PurePath, key: str, reason: str, *, written: tuple[str, ...] = ()
    ) -> None:
        self.path = path
        self.key = key
        self.reason = reason
        self.written = written
        done = f"; already written: {', '.join(written)}" if written else ""
        super().__init__(f"could not write {key} to {str(path)!r}: {reason}{done}")


class LoadFailedError(BackupError):
    """Loading a Dump started and failed, or the Dump was unreadable.

    ``reason`` is structured (an exit code or a failure class), never the
    dependency's text. ``database`` is the name of the database loaded into;
    ``exit_status`` the load's exit status when it exited, else ``None``.
    """

    def __init__(
        self, reason: str, *, database: str, exit_status: int | None = None
    ) -> None:
        self.reason = reason
        self.database = database
        self.exit_status = exit_status
        super().__init__(f"database load failed: {reason}")


class LoadReadinessError(BackupError):
    """The load-readiness check, run as the WordPress account, could not run.

    ``check_load`` answers as the account the load itself runs as, not the
    administrator (BL-E002-104, owner decision 2026-11-07), so a failure of
    the check is ``NOT_READY``: the Site cannot be loaded as it stands.
    ``reason`` is structured (a failure class), never the dependency's text;
    ``database`` is the database checked; ``source`` names the WordPress
    account's credential source, never a value.
    """

    exit_code: ClassVar[ExitCode] = ExitCode.NOT_READY

    def __init__(self, reason: str, *, database: str, source: str) -> None:
        self.reason = reason
        self.database = database
        self.source = source
        super().__init__(
            f"could not check that database {database!r} can be loaded as the "
            f"WordPress account (credentials from {source!r}): {reason}"
        )


class UnsafeCleanTargetError(RefusedError):
    """A Site Root that ``clean`` refuses to empty (a mistyped ``--wp-dir``).

    ``reason`` says which heuristic refused it: ``/``, the user's home, or a
    directory with neither ``wp-config.php`` nor ``wp-includes/``.
    """

    def __init__(self, site_root: PurePath, reason: str) -> None:
        self.site_root = site_root
        self.reason = reason
        super().__init__(f"refusing to clean {str(site_root)!r}: {reason}")


class UnreadableSiteRootError(RefusedError):
    """FR-7: the Site Root exists and cannot be listed or read (BL-E003-034).

    A refusal, not a failure: a target the guard cannot see is never treated
    as empty. Names the path and the ``OSError`` class only, never contents.
    A missing Site Root keeps :class:`InvalidSiteDescriptorError`.
    """

    def __init__(self, site_root: PurePath, error: str) -> None:
        self.site_root = site_root
        self.error = error
        super().__init__(
            f"the site root {str(site_root)!r} exists but cannot be read ({error}); "
            "nothing was changed"
        )


class CleanFailedError(BackupError):
    """``clean`` stopped at ``entry``; the entries after it were left in place."""

    def __init__(self, entry: PurePath, reason: str, *, removed: int) -> None:
        self.entry = entry
        self.reason = reason
        self.removed = removed
        super().__init__(
            f"could not remove {str(entry)!r}: {reason} "
            f"({removed} entries removed before it; the rest were left)"
        )


class SiteLockedError(BackupError):
    """Another Run holds the Site Lock (AD-19)."""

    exit_code: ClassVar[ExitCode] = ExitCode.LOCKED

    def __init__(
        self,
        site_root: PurePath,
        lock_path: PurePath,
        *,
        holder_run_id: str | None = None,
        holder_pid: int | None = None,
        holder_started_at: str | None = None,
        holder_hostname: str | None = None,
    ) -> None:
        self.site_root = site_root
        self.lock_path = lock_path
        self.holder_run_id = holder_run_id
        self.holder_pid = holder_pid
        self.holder_started_at = holder_started_at
        self.holder_hostname = holder_hostname
        holder = (
            f"held by run {holder_run_id}, pid {holder_pid} on "
            f"{holder_hostname or 'an unknown host'}, since {holder_started_at}"
            if holder_run_id is not None
            else "the holder could not be identified"
        )
        # UX-6 (BL-E002-103): what to do next, not only who holds it.
        if holder_pid is not None:
            # F3: the record is read without the lock, so for a moment it can
            # still name a dead holder whose lock a new Run has just taken.
            advice = (
                f"wait for that Run to finish, or check it with `ps -p {holder_pid}`; "
                "a lock whose process has gone is reclaimed by the next Run, and "
                "if that process has gone, another Run has just taken the lock "
                "and not yet recorded itself: try again shortly"
            )
        else:
            advice = "wait for the other Run to finish, then try again"
        super().__init__(
            f"site {str(site_root)!r} is locked ({holder}; lock file "
            f"{str(lock_path)!r}); {advice}. The lock covers Runs by the same "
            "OS user only"
        )


class SiteLockLocationError(BackupError):
    """The Site Lock's directory is unusable, or inside the Site Root (AD-19).

    The Run does not proceed unlocked.
    """

    exit_code: ClassVar[ExitCode] = ExitCode.NOT_READY

    def __init__(self, path: PurePath, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"site lock directory {str(path)!r}: {reason}")


class SiteLockReleaseError(BackupError):
    """A Site Lock handle was released more often than acquired. A defect."""

    def __init__(self, site_root: PurePath) -> None:
        self.site_root = site_root
        super().__init__(
            f"the site lock for {str(site_root)!r} was released more often than "
            "it was acquired"
        )


class RunTerminated(BackupError):
    """The Run received SIGTERM; raised so its ``finally`` chain runs (FR-28)."""

    def __init__(self, signal_name: str) -> None:
        self.signal_name = signal_name
        super().__init__(f"the run was terminated by {signal_name}")


class PreflightFailedError(BackupError):
    """A tool the Run needs is absent or unusable.

    ``minimum`` is the version the tool must reach, set when the failure is a
    too-old or unknown version, so the message states the floor.
    """

    exit_code: ClassVar[ExitCode] = ExitCode.NOT_READY

    def __init__(
        self,
        tool: str,
        needed_for: str,
        found: str,
        *,
        install_hint: str | None = None,
        minimum: str | None = None,
    ) -> None:
        self.tool = tool
        self.needed_for = needed_for
        self.found = found
        self.install_hint = install_hint
        self.minimum = minimum
        floor = f"; {minimum} or later is needed" if minimum else ""
        hint = f"\ninstall: {install_hint}" if install_hint else ""
        super().__init__(
            f"{tool} is needed for {needed_for}; found: {found}{floor}{hint}"
        )


class MalformedManifestError(BackupError):
    """A Manifest's JSON is not a valid v1 Manifest.

    ``field`` is the Manifest field at fault, as the message names it
    (``payload.sha256``, ``size``), or ``None`` when the whole document is
    unreadable; ``reason`` says what is wrong with it.
    """

    def __init__(self, reason: str, *, field: str | None) -> None:
        self.reason = reason
        self.field = field
        super().__init__(f"malformed manifest: {reason}")


class VerificationFailedError(BackupError):
    """An Archive failed verification; ``result`` says why.

    ``archive`` names the Archive the operator gave, when the raiser knows it,
    so a job verifying several can tell which failed (BL-E002-055).
    """

    exit_code: ClassVar[ExitCode] = ExitCode.VERIFICATION_FAILED

    def __init__(self, result: VerificationResult, archive: str | None = None) -> None:
        self.result = result
        self.archive = archive
        first = result.failures[0] if result.failures else None
        detail = (
            "no reason recorded"
            if first is None
            else first.reason.value
            if first.member is None
            else f"{first.reason.value} ({first.member!r})"
        )
        named = "" if archive is None else f" for {archive!r}"
        super().__init__(f"archive verification failed{named}: {detail}")


class PassphraseRejectedError(BackupError):
    """The passphrase cannot open the encrypted payload (``DecryptionError``).

    ``VERIFICATION_FAILED``: the payload could not be authenticated (Shawn,
    2026-11-03). Kept distinct from :class:`PayloadDamagedError` (ADR-0014).
    ``scheme`` names the encryption whose authentication failed (``age``):
    the cipher raises this from a stream and holds no Archive identity, so
    the Archive is named by the operation's own context (the Run's records
    and the caller's argument), not by a field here.
    """

    exit_code: ClassVar[ExitCode] = ExitCode.VERIFICATION_FAILED

    def __init__(self, *, scheme: str) -> None:
        self.scheme = scheme
        super().__init__(
            "authentication failed: the passphrase cannot decrypt this archive"
        )


class PayloadDamagedError(BackupError):
    """The encrypted payload is damaged, truncated or not ``age``.

    Database's ``CorruptDumpError``.

    ``age`` authenticates every chunk, so a tampered payload lands here. So
    does an outer Manifest that disagrees with the inner Manifest the payload
    carries (ADR-0024 amendment, 2026-11-06): ``field`` names the first
    differing field's path, never a value.
    """

    exit_code: ClassVar[ExitCode] = ExitCode.VERIFICATION_FAILED

    def __init__(self, field: str | None = None) -> None:
        self.field = field
        message = (
            "authentication failed: the encrypted payload is damaged or tampered with"
        )
        if field is not None:
            message += f" (the Manifest's {field} differs from the authenticated copy)"
        super().__init__(message)


class WorkFactorTooHighError(RefusedError):
    """The payload's scrypt work factor is above what this host will decrypt.

    Database's ``WorkFactorTooHighError`` (0.3.6): the ``age`` header asks
    for more scrypt work than the decrypting backend accepts (the binder,
    0.1.3, and stock ``age`` both stop at log2 22), so it is refused before
    any work is done. Nothing says the payload is damaged, so this is not
    :class:`PayloadDamagedError`, and it is not a verification failure: it
    is ``REFUSED``, a fixed limit declining an input, with nothing changed.
    ``wpbackup2`` 1.1.2 and later write 18, so an Archive it wrote does not
    reach it. ``scheme`` names the encryption (``age``).
    """

    def __init__(self, *, scheme: str) -> None:
        self.scheme = scheme
        super().__init__(
            "refused: the encrypted payload's scrypt work factor is above this "
            "host's limit (log2 22), so it was not decrypted; the payload is not "
            "known to be damaged. wpbackup2 writes 18: decrypt it with the tool "
            "that encrypted it"
        )


class ArchiveNotEncryptedError(UsageError):
    """``--authenticate`` was asked of an Archive with no encrypted payload.

    Nothing in a plain Archive can be authenticated, so passing would be the
    false reassurance BL-E002-060 removes. ``restore --require-encrypted``
    raises it too (``required=True``) for a plain Archive. ``archive`` names
    it, ``required`` says which, and ``arguments`` is the parameter at fault
    (``authenticate`` or ``require_encrypted``). A plain Archive restored
    with a passphrase is not refused (owner decision 59): it proceeds with
    WARNING ``archive_not_encrypted``.
    """

    def __init__(self, archive: str, *, required: bool = False) -> None:
        self.archive = archive
        self.required = required
        if required:
            # ``restore --require-encrypted`` (RT-1): a plain Archive where an
            # encrypted one was expected may be a substitution.
            super().__init__(
                f"archive {archive!r} is not encrypted, and --require-encrypted "
                "was given; it may have been substituted for an encrypted one",
                arguments=("require_encrypted",),
            )
            return
        super().__init__(
            f"archive {archive!r} is not encrypted, so there is nothing to "
            "authenticate; verify it without --authenticate",
            arguments=("authenticate",),
        )


class UnrecognisedArchiveError(BackupError):
    """The Archive is none of the shapes this build reads (AD-23)."""

    exit_code: ClassVar[ExitCode] = ExitCode.VERIFICATION_FAILED

    def __init__(self, found: tuple[str, ...]) -> None:
        self.found = found
        names = ", ".join(repr(name) for name in found) or "no members"
        super().__init__(f"unrecognised archive format; found {names}")


class UnsupportedFormatVersionError(BackupError):
    """The Manifest's ``format_version`` is not one this build reads."""

    exit_code: ClassVar[ExitCode] = ExitCode.VERIFICATION_FAILED

    def __init__(self, found: int) -> None:
        self.found = found
        super().__init__(f"unsupported archive format_version {found}")


class ArchiveNotFoundError(BackupError):
    """The Archive does not exist or cannot be opened.

    ``USAGE`` (BL-E002-074, owner decision 2026-11-07): the path the caller
    named is not there.
    """

    exit_code: ClassVar[ExitCode] = ExitCode.USAGE

    def __init__(self, path: PurePath) -> None:
        self.path = path
        super().__init__(f"archive {str(path)!r} does not exist or is unreadable")


class ArchiveReadError(BackupError):
    """An Archive could not be read to the member asked for (I/O or tar error)."""

    def __init__(self, path: PurePath, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"could not read archive {str(path)!r}: {reason}")


class ArchiveHasNoDumpError(RefusedError):
    """A Restore Mode names the database and the Archive holds no Dump."""

    def __init__(self, archive: str, backup_scope: str | None) -> None:
        self.archive = archive
        self.backup_scope = backup_scope
        scope = "a v0 archive" if backup_scope is None else f"scope {backup_scope!r}"
        super().__init__(
            f"archive {archive!r} holds no database dump ({scope}); "
            "restore its files with --mode files"
        )


class ExtractionRefusedError(RefusedError):
    """Extraction refused the Archive: an unsafe member, or no ``data`` filter.

    A refusal at a member means the Archive is not what it claims, so the
    message says so and how to check it (UX-7); one with no member is about
    this interpreter and says what to do in its ``reason``.
    """

    def __init__(self, member: str | None, reason: str) -> None:
        self.member = member
        self.reason = reason
        if member is None:
            super().__init__(f"extraction refused: {reason}")
            return
        super().__init__(
            f"extraction refused at member {member!r}: {reason}; the archive may "
            "be damaged or tampered with, so check it with "
            "`wpbackup2 verify --archive <archive>`"
        )


class MemberReadRefusedError(RefusedError):
    """A single-member read was refused: not a regular file, not indexed, too big."""

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"refused to read archive member {path!r}: {reason}")


class RunContextActiveError(RefusedError):
    """AD-16 — a Run was opened while another is active in this context."""

    def __init__(self, active_run_id: str) -> None:
        self.active_run_id = active_run_id
        super().__init__(
            f"a Run is already active (run_id {active_run_id}): join it, "
            "do not open a second"
        )


class UnknownInterruptedRunError(RefusedError):
    """FR-28 — an acknowledgement named a Run that is not the interrupted one.

    ``run_id`` is the Run named; ``recorded_run_id`` the interrupted Run the
    Site's journal records, or ``None`` when it records none. Nothing was
    resolved.
    """

    def __init__(self, run_id: str, *, recorded_run_id: str | None) -> None:
        self.run_id = run_id
        self.recorded_run_id = recorded_run_id
        if recorded_run_id is None:
            super().__init__(
                f"no interrupted run {run_id} is recorded for this Site; "
                "nothing was resolved"
            )
            return
        super().__init__(
            f"the interrupted run is {recorded_run_id}, not {run_id}; "
            "nothing was resolved"
        )


class AdaptersNotBoundError(BackupError):
    """ADR-0016 — a Run's adapters were read before they were bound. A defect."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        super().__init__(f"the adapters of Run {run_id} were read before bind_adapters")


class AdaptersAlreadyBoundError(BackupError):
    """ADR-0016 — a Run's adapters were bound a second time. A defect."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        super().__init__(f"the adapters of Run {run_id} are already bound")


class UnboundAdapterError(BackupError):
    """A port whose real implementation has not landed was called. A defect.

    No plan reaches a placeholder-bound port unless the feature behind it was
    supplied, so this is a programming error, never a user's: ``port`` names
    the Protocol method called and ``supplier`` the story or epic that binds
    the real implementation.
    """

    def __init__(self, port: str, *, supplier: str) -> None:
        self.port = port
        self.supplier = supplier
        super().__init__(
            f"{port} has no implementation bound yet ({supplier} binds it)"
        )


class StoredUrlMissingError(BackupError):
    """FR-12: the loaded database has no ``siteurl`` or ``home`` value to replace.

    Raised before any replacement, so nothing was rewritten.
    """

    def __init__(self, option: str) -> None:
        self.option = option
        super().__init__(
            f"the restored database has no {option!r} option value, so the content "
            "URLs cannot be relocated; nothing was replaced"
        )


class StoredUrlInvalidError(RefusedError):
    """FR-12: the loaded database's ``siteurl`` or ``home`` is not a URL to replace.

    The value came from the Archive and would become a ``wp search-replace``
    argument, so it is refused before any replacement. It is not echoed: it is
    untrusted text, and may hold terminal control characters.
    """

    def __init__(self, option: str, reason: str) -> None:
        self.option = option
        self.reason = reason
        super().__init__(
            f"the restored database's {option!r} option value is not a URL this "
            f"tool will pass to WP-CLI ({reason}); nothing was replaced"
        )


class WpCliFailedError(BackupError):
    """FR-12: a ``wp`` call exited non-zero or could not be started.

    ``stderr_tail`` is the last lines of its stderr, redacted by the adapter
    before it is put here; ``exit_status`` is ``None`` when ``wp`` never ran.
    """

    def __init__(self, step: str, exit_status: int | None, stderr_tail: str) -> None:
        self.step = step
        self.exit_status = exit_status
        self.stderr_tail = stderr_tail
        status = "could not be run" if exit_status is None else f"exited {exit_status}"
        tail = f": {stderr_tail}" if stderr_tail else ""
        super().__init__(f"WP-CLI ({step}) {status}{tail}")


class WpCliOutputUnrecognisedError(BackupError):
    """FR-12: WP-CLI's output did not have the shape this package reads.

    ``replaced_maybe`` is ``False`` for a read or preview step, which precedes
    every replacement, and ``True`` for a ``replace`` or ``recount`` step, which
    follows one: the replacement may have been made, and the journal says so.
    """

    def __init__(
        self, step: str, reason: str, *, wp_cli_version: str, replaced_maybe: bool
    ) -> None:
        self.step = step
        self.reason = reason
        self.wp_cli_version = wp_cli_version
        self.replaced_maybe = replaced_maybe
        outcome = (
            "the replacement may have been made; see the journal"
            if replaced_maybe
            else "nothing was replaced"
        )
        super().__init__(
            f"WP-CLI {wp_cli_version} ({step}) printed output this tool does not "
            f"recognise: {reason}; {outcome}"
        )


class InterferingReplacementsError(RefusedError):
    """FR-12: the URL pairs would rewrite each other's output in every order.

    Raised before any replacement. ``pairs`` holds (old, new) URLs, which are
    not secrets.
    """

    def __init__(self, pairs: tuple[tuple[str, str], ...]) -> None:
        self.pairs = pairs
        shown = "; ".join(f"{old!r} -> {new!r}" for old, new in pairs)
        super().__init__(
            f"refused to rewrite content URLs: the replacements {shown} would "
            "rewrite each other's results in any order; nothing was replaced. "
            "Rewrite them with `wp search-replace` yourself, through a temporary "
            "placeholder string"
        )


class SearchReplaceIncompleteError(BackupError):
    """FR-12: after the replacement the old URL is still found ``remaining`` times."""

    def __init__(self, old: str, new: str, remaining: int) -> None:
        self.old = old
        self.new = new
        self.remaining = remaining
        super().__init__(
            f"{remaining} occurrence(s) of {old!r} remain after replacing it "
            f"with {new!r}"
        )


class WpCliConfigFoundError(RefusedError):
    """AD-11: a ``wp-cli.yml`` WP-CLI would read sits in an ancestor of its directory.

    WP-CLI reads ``wp-cli.yml``/``wp-cli.local.yml`` from its working
    directory and upwards, and such a file can ``require:`` PHP. Raised before
    the ``wp`` call, so nothing was replaced by it.
    """

    def __init__(self, path: PurePath) -> None:
        self.path = path
        super().__init__(
            f"refused to run WP-CLI: {str(path)!r} would configure it (it can load "
            "PHP); remove the file or run the restore from another state directory"
        )


class UnsupportedConfigLocationError(RefusedError):
    """FR-29, AD-11: the Manifest's ``config_path`` is not a place WordPress reads.

    ``value`` is the Manifest's text as found, and ``allowed`` the two
    ``ConfigLocation`` values. The value is never joined to a path.
    """

    def __init__(self, value: str, *, allowed: tuple[str, ...]) -> None:
        self.value = value
        self.allowed = allowed
        super().__init__(
            f"refused: the Archive's Manifest records config_path {value!r}, "
            f"which is not one of {', '.join(repr(a) for a in allowed)}; "
            "nothing was changed"
        )


class ConfigLocationConflictError(RefusedError):
    """FR-29: ``wp-config.php`` cannot be laid down where the Archive places it.

    ``path`` is the file or directory in the way; ``reason`` says why. Raised
    before any effect.
    """

    def __init__(self, path: PurePath, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(
            f"refused to lay down wp-config.php: {str(path)!r} {reason}; "
            "nothing was changed"
        )


class UnsupportedConfigValueError(RefusedError):
    """Owner decision 47: a ``wp-config.php`` setting this tool will not evaluate.

    ``key`` is the constant (``DB_HOST``) or variable (``$table_prefix``)
    whose value is a PHP expression other than a literal,
    ``getenv_docker('NAME', <literal>)`` or ``getenv('NAME')``; ``path`` is
    the file. Neither the expression's source text nor any value is carried:
    it may compute a credential. Raised before any connection.
    """

    def __init__(self, key: str, *, path: PurePath) -> None:
        self.key = key
        self.path = path
        super().__init__(
            f"refused: {key} in {str(path)!r} is a PHP expression this tool does "
            "not evaluate; define it as a literal, or as getenv_docker('NAME', "
            "'default') or getenv('NAME'); nothing was changed"
        )


class EnvironmentDrivenTargetError(RefusedError):
    """ADR-0027: a relocation would rewrite a ``DB_*`` the target reads from
    the environment.

    A restore without relocation loads straight into the database the
    environment names and writes no ``DB_*`` (owner decisions 47 and 49).
    A relocation (``--new-db-host``, ``--new-db-name``, ``--new-db-user``,
    ``--new-db-password``) rewrites the keys it moves in ``wp-config.php``,
    which cannot keep a ``getenv_docker()``/``getenv()`` definition; it is
    refused before any change, a Dry Run included. ``keys`` are the
    constants, ``variables`` the environment variables they read (names
    only, never values), ``path`` the ``wp-config.php`` and ``operation``
    what was refused.
    """

    def __init__(
        self,
        *,
        keys: tuple[str, ...],
        variables: tuple[str, ...],
        path: PurePath,
        operation: str,
    ) -> None:
        self.keys = keys
        self.variables = variables
        self.path = path
        self.operation = operation
        read = ", ".join(f"{k} from {v}" for k, v in zip(keys, variables, strict=True))
        super().__init__(
            f"refused: {str(path)!r} reads {read} from the environment, and "
            f"{operation} would rewrite those definitions as fixed values "
            "(docs/adr/0027-restore-into-an-environment-driven-database.md); "
            "change the container's environment instead and restore without "
            "the --new-db-* flags, or define the DB_* constants as literals; "
            "nothing was changed"
        )


class UndefinedConfigFunctionError(RefusedError):
    """Owner decision 48: ``--overwrite-wp-config`` would carry a call to a
    function the Archive's ``wp-config.php`` does not define.

    The target reads ``keys`` through ``function`` (``getenv_docker``, the
    official ``wordpress`` image's helper, which PHP does not define), and
    the Archive's config, which replaces the target's, does not define it
    before that use, so PHP would stop with "Call to undefined function".
    Refused before any change, a Dry Run included. ``path`` is the target's
    ``wp-config.php``. Whether the Archive's config declares it is
    ``l3io-wordpress-config``'s lexical ``declares_function`` (0.1.7), with
    the limits that library documents.
    """

    def __init__(self, *, function: str, keys: tuple[str, ...], path: PurePath) -> None:
        self.function = function
        self.keys = keys
        self.path = path
        super().__init__(
            f"refused: {str(path)!r} reads {', '.join(keys)} through {function}(), "
            f"and the Archive's wp-config.php, which --overwrite-wp-config would "
            f"put in its place, does not define {function}() before that use, so "
            f'PHP would stop with "Call to undefined function {function}()"; omit '
            "--overwrite-wp-config to keep the target's wp-config.php; nothing "
            "was changed"
        )


class RootRestoreRefusedError(UsageError):
    """A restore as root that cannot run as the Site Root's owner (owner decision 58).

    Started as root, the CLI reads what only root can read, then runs the rest
    of the Run as the uid and gid that own the Site Root. Refused (``USAGE``,
    before the Run opens) when the Site Root is owned by root, which leaves
    no owner to run as, and when a library caller runs as root: the library
    never changes its host's user, so the host runs it as the site's owner.
    ``site_root`` names the Site Root and ``reason`` why; ``arguments`` is
    ``("site_root",)``.
    """

    def __init__(self, *, site_root: str, reason: str) -> None:
        self.site_root = site_root
        self.reason = reason
        super().__init__(
            f"refused to restore as root into {site_root!r}: {reason}",
            arguments=("site_root",),
        )


class RootBackupRefusedError(UsageError):
    """A backup as root that cannot run as the Site Root's owner (decisions 67, 68).

    Started as root, the CLI reads what only root can read (the passphrase,
    the database credentials), then runs the rest of the Run as the uid and
    gid that own the Site Root, as a restore does. Refused (``USAGE``,
    before the Run opens) when the Site Root is owned by root, which leaves
    no owner to run as, and when a library caller runs as root: the library
    never changes its host's user. ``site_root`` names the Site Root and
    ``reason`` why; ``arguments`` is ``("site_root",)``.
    """

    def __init__(self, *, site_root: str, reason: str) -> None:
        self.site_root = site_root
        self.reason = reason
        super().__init__(
            f"refused to back up as root from {site_root!r}: {reason}",
            arguments=("site_root",),
        )


class ArchiveDirectoryNotWritableError(RefusedError):
    """A backup started as root whose Archive directory its site owner cannot write.

    Owner decision 67: the backup runs as the Site Root's owner, so the
    Archive is written by that user. Refused after the switch and before the
    Run opens, a Dry Run too. ``directory`` is the Archive directory (the
    target's directory, ``--archive-dir``, or the default one), ``uid`` and
    ``user`` the owner it was checked for (``user`` ``None`` when the uid has
    no password entry).
    """

    def __init__(self, *, directory: str, uid: int, user: str | None) -> None:
        self.directory = directory
        self.uid = uid
        self.user = user
        who = f"uid {uid}" if user is None else f"{user} (uid {uid})"
        super().__init__(
            f"the Archive directory {directory!r} is not writable by the Site "
            f"Root's owner, {who}, whom a backup started as root runs as; give "
            "that user write access to it, or name another with --archive-dir "
            "or --archive; nothing was changed"
        )


class PruneFailedError(BackupError):
    """Some Archives a prune chose could not be removed (owner decision 83).

    The others were. ``failures`` holds each ``(path, reason)``; ``exit_code``
    is ``FAILED``.
    """

    def __init__(self, *, failures: tuple[tuple[str, str], ...]) -> None:
        self.failures = failures
        named = "; ".join(f"{path!r}: {reason}" for path, reason in failures[:3])
        super().__init__(
            f"{len(failures)} archive(s) chosen to be pruned were left in place: "
            f"{named}"
        )


class TargetNotWritableError(RefusedError):
    """A restore would write where the user it runs as cannot (Phase 3 finding).

    Found by the target survey before any effect, a Dry Run too: the Site
    Root, an existing directory the Archive's files go into, an existing file
    they replace, every directory ``--cleanfirst`` clears, or where a
    laid-down ``wp-config.php`` is created. ``paths`` are those paths, sorted;
    ``uid`` is the effective uid the check was made for (``None`` when not
    known). ``--force`` does not waive it.
    """

    #: How many paths the message names; ``paths`` holds them all.
    SHOWN: ClassVar[int] = 3

    def __init__(self, *, paths: tuple[str, ...], uid: int | None) -> None:
        self.paths = paths
        self.uid = uid
        named = ", ".join(repr(p) for p in paths[: self.SHOWN])
        more = len(paths) - self.SHOWN
        if more > 0:
            named += f" and {more} more"
        who = "the user this Run runs as" if uid is None else f"uid {uid}"
        super().__init__(
            f"the restore target is not writable by {who}: {named}; run the "
            "restore as root (in the Docker image, PUID=0), which switches to "
            "the Site Root's owner, or as that owner, or give "
            f"{who} write access (for example with chown -R on the Site Root); "
            f"{NEVER_CHANGES_OWNERSHIP}; nothing was changed"
        )


#: What every refusal over a path's owner or mode says (owner decision 87).
NEVER_CHANGES_OWNERSHIP: Final = "wpbackup2 never changes ownership"


class PathNotAccessibleError(RefusedError):
    """A path the Run needs is not readable or writable by its user (decision 87).

    Checked by the ``wpbackup2`` command before the Run opens, a Dry Run too,
    after any switch to the Site Root's owner: the Archive directory (write,
    for a backup or a prune), the Archive (read, for a restore or a verify),
    the per-user state directory (write), ``TMPDIR`` when it is set (write),
    a restore's ``--snapshot-dir`` (write) and a backup's Site Root (read).
    Nothing is changed: ownership is the operator's to fix, and the message
    says how. ``path`` is the path needed and ``checked`` the one examined:
    the path itself, its nearest existing ancestor when it does not exist
    yet (where it would be made), or the first folder above it the user
    cannot enter. ``role`` names what it is, ``access`` is ``"read"`` or
    ``"write"``; ``uid`` and ``gid`` are the Run's effective ids; ``owner_uid``,
    ``owner_gid`` and ``mode`` are ``checked``'s (``None`` when it could not be
    examined).
    """

    def __init__(
        self,
        *,
        path: str,
        role: str,
        access: Literal["read", "write"],
        uid: int,
        gid: int,
        checked: str,
        owner_uid: int | None,
        owner_gid: int | None,
        mode: int | None,
    ) -> None:
        self.path = path
        self.role = role
        self.access = access
        self.uid = uid
        self.gid = gid
        self.checked = checked
        self.owner_uid = owner_uid
        self.owner_gid = owner_gid
        self.mode = mode
        verb = "readable" if access == "read" else "writable"
        me = f"{uid}:{gid}"
        where = "" if checked == path else f"checked at {checked!r}, "
        if owner_uid is None or owner_gid is None or mode is None:
            facts = f"{where}whose owner could not be read"
        else:
            facts = f"{where}owned by {owner_uid}:{owner_gid}, mode {mode:04o}"
        if owner_uid == uid:
            fixes = [f"give its owner, {me}, {access} access to it with chmod"]
        else:
            fixes = [
                f"give {me} {access} access to it (for example chown -R {me} on "
                "the host folder or volume mounted there)"
            ]
        if owner_uid not in (None, 0, uid):
            owner = f"{owner_uid}:{owner_gid}"
            fixes.append(
                f"run wpbackup2 as {owner} (in the Docker image, PUID={owner_uid} "
                f"PGID={owner_gid}, or --user {owner})"
            )
        super().__init__(
            f"the {role} {path!r} is not {verb} by uid {me} ({facts}); "
            f"{NEVER_CHANGES_OWNERSHIP}: {', or '.join(fixes)}; nothing was changed"
        )


class PrivilegeDropError(RefusedError):
    """Switching to the Site Root's owner did not hold (owner decisions 58, 67).

    After ``setgroups``, ``setgid`` and ``setuid`` the process's real,
    effective and saved ids are read back, and regaining root must fail.
    Anything else stops the Run before it opens. ``uid``/``gid`` are the ids
    asked for; ``step`` names what did not hold.
    """

    def __init__(self, *, uid: int, gid: int, step: str) -> None:
        self.uid = uid
        self.gid = gid
        self.step = step
        super().__init__(
            f"could not switch to the Site Root's owner (uid {uid}, gid {gid}): "
            f"{step}; nothing was changed"
        )


class InstallationNotReadableError(RefusedError):
    """The Site Root's owner cannot read the installation a root Run runs from.

    Owner decisions 58 and 67: a Run started as root switches to the Site
    Root's owner, and Python loads modules on first use, so a part of the
    installation the owner cannot read (an interpreter or virtual environment
    under root's home directory, as ``uv tool install`` or ``pipx install``
    run as root leave it) would fail a later import mid-Run. Refused right
    after the switch, before the Run opens. ``path`` is the first directory
    the owner cannot read, ``uid`` and ``user`` the owner (``user`` ``None``
    when the uid has no password entry).
    """

    def __init__(self, *, path: str, uid: int, user: str | None) -> None:
        self.path = path
        self.uid = uid
        self.user = user
        who = f"uid {uid}" if user is None else f"{user} (uid {uid})"
        super().__init__(
            f"the installation directory {path!r} is not readable by the Site "
            f"Root's owner, {who}, whom a Run started as root runs as; install "
            "wpbackup2 and its Python where that user can read them (not under "
            "root's home directory), or run wpbackup2 as that user; nothing was "
            "changed"
        )


class NoArchiveForSiteError(UsageError):
    """``--archive latest`` found no Archive of this Site (owner decision 64).

    ``directory`` is the Archive directory searched, ``considered`` how many
    Archives in it were read, and ``site`` what identifies this Site (its
    Site Root and database), as the Manifest records it. ``arguments`` is
    ``("archive",)``.
    """

    def __init__(self, *, directory: PurePath, considered: int, site: str) -> None:
        self.directory = directory
        self.considered = considered
        self.site = site
        super().__init__(
            f"--archive latest: none of the {considered} archives read in "
            f"{str(directory)!r} is a backup of this site ({site}); name the "
            "archive, or point --archive-dir at this site's archives",
            arguments=("archive",),
        )


def exit_code_of(exc: BaseException) -> ExitCode:
    """``exc``'s ``exit_code`` when it is a :class:`BackupError`, else ``FAILED``.

    A non-``BackupError`` reaching a boundary is a defect, so it is a plain
    failure; no class is matched by name.
    """
    return type(exc).exit_code if isinstance(exc, BackupError) else ExitCode.FAILED
