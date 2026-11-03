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

from pathlib import PurePath
from typing import TYPE_CHECKING, ClassVar, Literal

from l3io.wp.backup.domain.exit_codes import ExitCode

if TYPE_CHECKING:
    from l3io.wp.backup.domain.archive_format import VerificationResult

__all__ = [
    "AdaptersAlreadyBoundError",
    "AdaptersNotBoundError",
    "AdminCredentialsRequiredError",
    "ArchiveExistsError",
    "ArchiveHasNoDumpError",
    "ArchiveNotFoundError",
    "ArchiveReadError",
    "ArchiveWriteError",
    "BackupError",
    "ConfigWriteFailedError",
    "CredentialsError",
    "DatabaseRefusedError",
    "DatabaseUnreachableError",
    "DumpFailedError",
    "ExtractionRefusedError",
    "InvalidSiteDescriptorError",
    "MalformedManifestError",
    "MemberReadRefusedError",
    "MissingExtraError",
    "PassphraseRequiredError",
    "PassphraseUnavailableError",
    "PreflightFailedError",
    "ProvisioningFailedError",
    "RefusedError",
    "RunContextActiveError",
    "SiteLockedError",
    "UnrecognisedArchiveError",
    "UnsafeMemberPathError",
    "UnsupportedFormatVersionError",
    "UnsupportedStorageSchemeError",
    "UsageError",
    "VerificationFailedError",
    "exit_code_of",
]


class BackupError(Exception):
    """The base of every error this package raises across a public boundary."""

    #: The process exit code this kind of failure ends a Run with.
    exit_code: ClassVar[ExitCode] = ExitCode.FAILED

    #: Set by the restore driver on any error escaping after the Safety
    #: Snapshot completed, so the failure output names it.
    snapshot_path: PurePath | None = None


class RefusedError(BackupError):
    """A check found a reason not to act, so nothing was done."""

    exit_code: ClassVar[ExitCode] = ExitCode.REFUSED


class UsageError(BackupError):
    """The caller supplied an invalid combination of arguments."""

    exit_code: ClassVar[ExitCode] = ExitCode.USAGE


class PassphraseUnavailableError(UsageError):
    """AD-26 — passphrase encryption was requested and no usable one resolved.

    ``variable`` is the environment variable consulted, never its value.
    """

    def __init__(self, variable: str, *, reason: str) -> None:
        self.variable = variable
        self.reason = reason
        super().__init__(
            f"no usable passphrase: {reason} (pass passphrase=, set {variable}, "
            "or run on a terminal so it can be prompted for)"
        )


class InvalidSiteDescriptorError(BackupError):
    """The Site Descriptor could not be built from what was given."""

    def __init__(self, reason: str, *, paths: tuple[str, ...] = ()) -> None:
        self.reason = reason
        self.paths = paths
        where = f" ({', '.join(paths)})" if paths else ""
        super().__init__(f"invalid site: {reason}{where}")


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
    """The operation needs administrator credentials that were not given."""

    def __init__(self, source: str, *, flags: tuple[str, ...]) -> None:
        self.flags = flags
        super().__init__(
            source, reason=f"administrator credentials required: {', '.join(flags)}"
        )


class DatabaseUnreachableError(BackupError):
    """The database server could not be reached."""

    exit_code: ClassVar[ExitCode] = ExitCode.UNREACHABLE

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        super().__init__(f"database server {host}:{port} could not be reached")


class DumpFailedError(BackupError):
    """The database dump started and failed."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"database dump failed: {reason}")


class ArchiveWriteError(BackupError):
    """The Archive could not be written (I/O failure, ``ENOSPC`` included)."""

    def __init__(self, path: PurePath, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"could not write archive {str(path)!r}: {reason}")


class ArchiveExistsError(RefusedError):
    """The Archive's final name already exists at its destination."""

    def __init__(self, path: PurePath) -> None:
        self.path = path
        super().__init__(f"archive {str(path)!r} already exists")


class MissingExtraError(BackupError):
    """An optional feature was used without its extra installed (AD-8)."""

    exit_code: ClassVar[ExitCode] = ExitCode.NOT_READY

    def __init__(self, extra: str) -> None:
        self.extra = extra
        super().__init__(
            f"this feature needs the {extra!r} extra: "
            f"pip install 'l3io-wordpress-backup[{extra}]'"
        )


class DatabaseRefusedError(RefusedError):
    """A database check found blockers; ``findings`` are codes only."""

    def __init__(self, findings: tuple[str, ...]) -> None:
        self.findings = findings
        super().__init__(f"database operation refused: {', '.join(findings)}")


class PassphraseRequiredError(RefusedError):
    """Encryption was requested, or the Archive is encrypted, and no passphrase."""

    def __init__(self, operation: Literal["backup", "restore"]) -> None:
        self.operation = operation
        super().__init__(f"{operation} needs a passphrase and none was resolved")


class ProvisioningFailedError(BackupError):
    """Provisioning started and failed part-way."""

    def __init__(self, reason: str, *, completed: tuple[str, ...] = ()) -> None:
        self.reason = reason
        self.completed = completed
        done = f" (completed: {', '.join(completed)})" if completed else ""
        super().__init__(f"provisioning failed: {reason}{done}")


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


class SiteLockedError(BackupError):
    """Another Run holds the Site Lock."""

    exit_code: ClassVar[ExitCode] = ExitCode.LOCKED

    def __init__(
        self,
        site_root: PurePath,
        lock_path: PurePath,
        *,
        holder_run_id: str | None = None,
        holder_pid: int | None = None,
        holder_started_at: str | None = None,
    ) -> None:
        self.site_root = site_root
        self.lock_path = lock_path
        self.holder_run_id = holder_run_id
        self.holder_pid = holder_pid
        self.holder_started_at = holder_started_at
        holder = (
            f"run {holder_run_id}, pid {holder_pid}, since {holder_started_at}"
            if holder_run_id is not None
            else "holder unknown"
        )
        super().__init__(f"site {str(site_root)!r} is locked ({holder})")


class PreflightFailedError(BackupError):
    """A tool the Run needs is absent or unusable."""

    exit_code: ClassVar[ExitCode] = ExitCode.NOT_READY

    def __init__(
        self,
        tool: str,
        needed_for: str,
        found: str,
        *,
        install_hint: str | None = None,
    ) -> None:
        self.tool = tool
        self.needed_for = needed_for
        self.found = found
        self.install_hint = install_hint
        hint = f"\ninstall: {install_hint}" if install_hint else ""
        super().__init__(f"{tool} is needed for {needed_for}; found: {found}{hint}")


class MalformedManifestError(BackupError):
    """A Manifest's JSON is not a valid v1 Manifest."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"malformed manifest: {reason}")


class VerificationFailedError(BackupError):
    """An Archive failed verification; ``result`` says why."""

    exit_code: ClassVar[ExitCode] = ExitCode.VERIFICATION_FAILED

    def __init__(self, result: VerificationResult) -> None:
        self.result = result
        first = result.failures[0] if result.failures else None
        detail = (
            "no reason recorded"
            if first is None
            else first.reason.value
            if first.member is None
            else f"{first.reason.value} ({first.member!r})"
        )
        super().__init__(f"archive verification failed: {detail}")


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
    """The Archive does not exist or cannot be opened."""

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
        super().__init__(f"archive {archive!r} holds no database dump ({scope})")


class ExtractionRefusedError(RefusedError):
    """Extraction refused the Archive: an unsafe member, or no ``data`` filter."""

    def __init__(self, member: str | None, reason: str) -> None:
        self.member = member
        self.reason = reason
        where = "" if member is None else f" at member {member!r}"
        super().__init__(f"extraction refused{where}: {reason}")


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


def exit_code_of(exc: BaseException) -> ExitCode:
    """``exc``'s ``exit_code`` when it is a :class:`BackupError`, else ``FAILED``.

    A non-``BackupError`` reaching a boundary is a defect, so it is a plain
    failure; no class is matched by name.
    """
    return type(exc).exit_code if isinstance(exc, BackupError) else ExitCode.FAILED
