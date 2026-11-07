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
    "ArchiveNotEncryptedError",
    "ArchiveNotFoundError",
    "ArchiveReadError",
    "ArchiveWriteError",
    "BackupError",
    "CleanFailedError",
    "ConfigWriteFailedError",
    "CredentialsError",
    "DatabaseRefusedError",
    "DatabaseUnreachableError",
    "DumpFailedError",
    "ExtractionRefusedError",
    "FlagCombinationRejectedError",
    "IncompleteRelocationCredentialsError",
    "InterferingReplacementsError",
    "InvalidRelocationValueError",
    "InvalidSiteDescriptorError",
    "LoadFailedError",
    "MalformedManifestError",
    "MemberReadRefusedError",
    "MissingExtraError",
    "PassphraseRejectedError",
    "PassphraseRequiredError",
    "PassphraseUnavailableError",
    "PayloadDamagedError",
    "PreflightFailedError",
    "ProvisioningFailedError",
    "RefusedError",
    "RelocationNeedsDatabaseRestoreError",
    "RunContextActiveError",
    "RunTerminated",
    "SearchReplaceIncompleteError",
    "SiteLockLocationError",
    "SiteLockReleaseError",
    "SiteLockedError",
    "StoredUrlMissingError",
    "UnboundAdapterError",
    "UnreadableSiteRootError",
    "UnrecognisedArchiveError",
    "UnsafeCleanTargetError",
    "UnsafeMemberPathError",
    "UnsupportedFormatVersionError",
    "UnsupportedStorageSchemeError",
    "UsageError",
    "VerificationFailedError",
    "WpCliConfigFoundError",
    "WpCliFailedError",
    "WpCliOutputUnrecognisedError",
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


class FlagCombinationRejectedError(UsageError):
    """AD-25 — restore flags that express no Restore Mode.

    The message is the flag table's own entry: why the combination was
    rejected and the combination that would express a real Mode.
    """

    def __init__(self, *, reason: str, use_instead: str) -> None:
        self.reason = reason
        self.use_instead = use_instead
        super().__init__(f"{reason}; {use_instead}")


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


class InvalidRelocationValueError(UsageError):
    """FR-11 — a relocation flag's value breaks a rule.

    ``flag`` and ``rule`` name it; the value is never echoed.
    """

    def __init__(self, flag: str, rule: str) -> None:
        self.flag = flag
        self.rule = rule
        super().__init__(f"{flag} {rule}")


class IncompleteRelocationCredentialsError(UsageError):
    """FR-11 — half of the relocated account was given.

    Keeping the other half from the Archive would mix two accounts.
    """

    def __init__(self, *, given: str, missing: str) -> None:
        self.given = given
        self.missing = missing
        super().__init__(
            f"{given} needs {missing}: the relocated account is a user and "
            "password pair, and keeping one half from the Archive would mix "
            "two accounts"
        )


class RelocationNeedsDatabaseRestoreError(UsageError):
    """FR-11 — relocation flags with a Restore Mode that restores no database.

    Such a Mode has no cutover, so every ``--new-*`` value would be accepted
    and discarded. ``mode`` is the Restore Mode's CLI spelling.
    """

    def __init__(self, *, flags: tuple[str, ...], mode: str) -> None:
        self.flags = flags
        self.mode = mode
        super().__init__(
            f"{', '.join(flags)} need a Restore Mode that restores the database; "
            f"Restore Mode {mode} restores none"
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
    dependency's text.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"database load failed: {reason}")


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
        super().__init__(
            f"site {str(site_root)!r} is locked ({holder}; lock file "
            f"{str(lock_path)!r})"
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


class PassphraseRejectedError(BackupError):
    """The passphrase cannot open the encrypted payload (``DecryptionError``).

    ``VERIFICATION_FAILED``: the payload could not be authenticated (Shawn,
    2026-11-03). Kept distinct from :class:`PayloadDamagedError` (ADR-0014).
    """

    exit_code: ClassVar[ExitCode] = ExitCode.VERIFICATION_FAILED

    def __init__(self) -> None:
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


class ArchiveNotEncryptedError(UsageError):
    """``--authenticate`` was asked of an Archive with no encrypted payload.

    Nothing in a plain Archive can be authenticated, so passing would be the
    false reassurance BL-E002-060 removes. ``restore --require-encrypted``
    raises it too (``required=True``) for a plain Archive.
    """

    def __init__(self, archive: str, *, required: bool = False) -> None:
        self.archive = archive
        if required:
            # ``restore --require-encrypted`` (RT-1): a plain Archive where an
            # encrypted one was expected may be a substitution.
            super().__init__(
                f"archive {archive!r} is not encrypted, and --require-encrypted "
                "was given; it may have been substituted for an encrypted one"
            )
            return
        super().__init__(
            f"archive {archive!r} is not encrypted, so there is nothing to "
            "authenticate; verify it without --authenticate"
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


def exit_code_of(exc: BaseException) -> ExitCode:
    """``exc``'s ``exit_code`` when it is a :class:`BackupError`, else ``FAILED``.

    A non-``BackupError`` reaching a boundary is a defect, so it is a plain
    failure; no class is matched by name.
    """
    return type(exc).exit_code if isinstance(exc, BackupError) else ExitCode.FAILED
