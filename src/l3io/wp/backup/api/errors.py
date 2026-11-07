"""AD-14 — the library's exceptions, and the one boundary that translates.

Every :class:`BackupError` subclass the package defines is re-exported here
by identity from the module that defines it; nothing is redefined, and there
is one base. ``tests/contracts/test_api_exception_exports.py`` walks the
package and fails on a class added anywhere without an export line here, so
**an epic that adds an error class adds its import and its ``__all__`` entry
to this module in the same change.**

:func:`public_boundary` wraps each entry point in ``api/operations.py``. A
``BackupError`` passes through untouched; any other ``Exception`` is a defect
and arrives as :class:`UnexpectedError` with the original on ``__cause__``.
A ``BackupError`` this module does not export (a private class) is wrapped
the same way, so no undocumented type reaches a caller.
``BaseException``s that are not ``Exception`` (``KeyboardInterrupt``,
``SystemExit``) belong to the host and pass through.

Nothing here redacts (ADR-0018), because nothing here writes a dependency's
text: the ``unexpected_exception`` record carries the traceback **frames**
with every non-package exception's message withheld (``cli.exit.render_failure``,
the rule the CLI's own failure report applies), never ``exc_info``. The Run's
registry is closed by the time this boundary logs, so a record that carried the
original's text could not be redacted. The original is still on ``__cause__``,
for the host, and so are its frames' local variables (``docs/api.md``).
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from typing import ParamSpec, TypeVar

import structlog

from l3io.wp.backup.adapters.crypto import EncryptionConfigError
from l3io.wp.backup.adapters.database import (
    DatabaseAuthPluginRefusedError,
    DatabaseTlsHandshakeError,
    DatabaseTlsVerificationError,
)
from l3io.wp.backup.adapters.journal import CorruptJournalError, JournalWriteError
from l3io.wp.backup.adapters.preflight import ChildOutputLimitError
from l3io.wp.backup.adapters.restore_runner import (
    ArchiveChangedError,
    TargetConfigMissingError,
)
from l3io.wp.backup.adapters.staging import (
    ConfigCutoverError,
    CutoverTargetUnusableError,
    InvalidLoadDatabaseNameError,
    StagingCheckFailedError,
)
from l3io.wp.backup.cli.exit import RUN_ID_ATTR, render_failure
from l3io.wp.backup.cli.restore import (
    ArchiveIdentityUnconfirmedError,
    DatabaseNotRestoredError,
    ExistingDatabaseUserError,
    NonEmptyTargetRefusal,
)
from l3io.wp.backup.core.exclusions import InvalidExclusionPatternError
from l3io.wp.backup.core.restore import (
    ArchiveHasNoConfigError,
    ExpansionRatioError,
    IncompleteRunError,
    InsufficientSpaceError,
    NoConfigForCutoverError,
    NonEmptyTargetError,
    SnapshotFailedError,
    TargetChangedError,
    UnsafeSnapshotLocationError,
)
from l3io.wp.backup.domain.errors import (
    AdaptersAlreadyBoundError,
    AdaptersNotBoundError,
    AdminCredentialsRequiredError,
    ArchiveExistsError,
    ArchiveHasNoDumpError,
    ArchiveNotEncryptedError,
    ArchiveNotFoundError,
    ArchiveReadError,
    ArchiveWriteError,
    BackupError,
    CleanFailedError,
    ConfigLocationConflictError,
    ConfigWriteFailedError,
    CredentialsError,
    CredentialTooShortError,
    DatabaseRefusedError,
    DatabaseUnreachableError,
    DumpFailedError,
    ExtractionRefusedError,
    FlagCombinationRejectedError,
    IncompleteRelocationCredentialsError,
    InterferingReplacementsError,
    InvalidRelocationValueError,
    InvalidSiteDescriptorError,
    LoadFailedError,
    MalformedManifestError,
    ManifestTooLargeError,
    MemberReadRefusedError,
    MissingExtraError,
    PassphraseMismatchError,
    PassphraseRejectedError,
    PassphraseRequiredError,
    PassphraseTooShortError,
    PassphraseUnavailableError,
    PayloadDamagedError,
    PreflightFailedError,
    ProvisioningFailedError,
    RefusedError,
    RelocationNeedsDatabaseRestoreError,
    RunContextActiveError,
    RunTerminated,
    SearchReplaceIncompleteError,
    SiteLockedError,
    SiteLockLocationError,
    SiteLockReleaseError,
    StoredUrlInvalidError,
    StoredUrlMissingError,
    UnboundAdapterError,
    UnknownInterruptedRunError,
    UnreadableSiteRootError,
    UnrecognisedArchiveError,
    UnsafeCleanTargetError,
    UnsafeMemberPathError,
    UnsupportedConfigLocationError,
    UnsupportedFormatVersionError,
    UnsupportedStorageSchemeError,
    UsageError,
    VerificationFailedError,
    WpCliConfigFoundError,
    WpCliFailedError,
    WpCliOutputUnrecognisedError,
)

__all__: list[str] = [
    "AdaptersAlreadyBoundError",
    "AdaptersNotBoundError",
    "AdminCredentialsRequiredError",
    "ArchiveChangedError",
    "ArchiveExistsError",
    "ArchiveHasNoConfigError",
    "ArchiveHasNoDumpError",
    "ArchiveIdentityUnconfirmedError",
    "ArchiveNotEncryptedError",
    "ArchiveNotFoundError",
    "ArchiveReadError",
    "ArchiveWriteError",
    "BackupError",
    "ChildOutputLimitError",
    "CleanFailedError",
    "ConfigCutoverError",
    "ConfigLocationConflictError",
    "ConfigWriteFailedError",
    "CorruptJournalError",
    "CredentialTooShortError",
    "CredentialsError",
    "CutoverTargetUnusableError",
    "DatabaseAuthPluginRefusedError",
    "DatabaseNotRestoredError",
    "DatabaseRefusedError",
    "DatabaseTlsHandshakeError",
    "DatabaseTlsVerificationError",
    "DatabaseUnreachableError",
    "DumpFailedError",
    "EncryptionConfigError",
    "ExistingDatabaseUserError",
    "ExpansionRatioError",
    "ExtractionRefusedError",
    "FlagCombinationRejectedError",
    "IncompleteRelocationCredentialsError",
    "IncompleteRunError",
    "InsufficientSpaceError",
    "InterferingReplacementsError",
    "InvalidExclusionPatternError",
    "InvalidLoadDatabaseNameError",
    "InvalidRelocationValueError",
    "InvalidSiteDescriptorError",
    "JournalWriteError",
    "LoadFailedError",
    "MalformedManifestError",
    "ManifestTooLargeError",
    "MemberReadRefusedError",
    "MissingExtraError",
    "NoConfigForCutoverError",
    "NonEmptyTargetError",
    "NonEmptyTargetRefusal",
    "PassphraseMismatchError",
    "PassphraseRejectedError",
    "PassphraseRequiredError",
    "PassphraseTooShortError",
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
    "SnapshotFailedError",
    "StagingCheckFailedError",
    "StoredUrlInvalidError",
    "StoredUrlMissingError",
    "TargetChangedError",
    "TargetConfigMissingError",
    "UnboundAdapterError",
    "UnexpectedError",
    "UnknownInterruptedRunError",
    "UnreadableSiteRootError",
    "UnrecognisedArchiveError",
    "UnsafeCleanTargetError",
    "UnsafeMemberPathError",
    "UnsafeSnapshotLocationError",
    "UnsupportedConfigLocationError",
    "UnsupportedFormatVersionError",
    "UnsupportedStorageSchemeError",
    "UsageError",
    "VerificationFailedError",
    "WpCliConfigFoundError",
    "WpCliFailedError",
    "WpCliOutputUnrecognisedError",
]

_log = structlog.get_logger(__name__)

_P = ParamSpec("_P")
_R = TypeVar("_R")


class UnexpectedError(BackupError):
    """A non-package exception reached the API boundary. It is a defect.

    ``cause_type`` is the original exception's fully qualified type name
    (``tarfile.ReadError``) and ``api_function`` the entry point it escaped;
    the original is on ``__cause__``. The message names both and never the
    original's text. ``exit_code`` is ``FAILED``, as the CLI treats any
    non-package exception.
    """

    def __init__(self, cause_type: str, *, api_function: str) -> None:
        self.cause_type = cause_type
        self.api_function = api_function
        super().__init__(
            f"{api_function} failed with an unexpected {cause_type} (a defect; "
            "the original exception is chained as __cause__)"
        )


#: Set on every function :func:`public_boundary` returns, so a check can tell
#: it from any other ``functools.wraps`` decorator.
BOUNDARY_MARK = "__l3io_public_boundary__"


def _as_is(text: str) -> str:
    """No redaction step: :func:`render_failure` returns no dependency text here."""
    return text


def _exported(cls: type[BaseException]) -> bool:
    """``cls`` is a class this module exports, by identity."""
    return globals().get(cls.__name__) is cls and cls.__name__ in __all__


def public_boundary(fn: Callable[_P, _R]) -> Callable[_P, _R]:
    """``fn`` with every exception it lets out an exported, documented type.

    An exported ``BackupError`` passes through unchanged, traceback intact, so
    wrapping is idempotent. Anything else that is an ``Exception`` (a
    non-package exception, an ``ExceptionGroup``, or a ``BackupError``
    subclass this module does not export, such as a private one) is a defect:
    it is logged once at ERROR as ``unexpected_exception`` and re-raised as
    ``UnexpectedError(...) from`` it. The record carries the failed Run's
    ``run_id`` when one was stamped on the exception; it follows the Run's
    ``run_finished``, which ``open_run`` wrote as the exception left it.

    Arguments that do not fit ``fn``'s signature (a missing required keyword
    such as ``backup``'s ``target``) are the caller's mistake, not a defect:
    the ``TypeError`` is raised as Python raises it, before anything runs.
    """
    name = fn.__name__
    signature = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        signature.bind(*args, **kwargs)
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # RunTerminated: mapped here (exported, re-raised)
            if isinstance(exc, BackupError) and _exported(type(exc)):
                raise
            cause_type = f"{type(exc).__module__}.{type(exc).__qualname__}"
            run_id = getattr(exc, RUN_ID_ATTR, None)
            run_fields: dict[str, object] = {} if run_id is None else {"run_id": run_id}
            _log.error(
                "unexpected_exception",
                cause_type=cause_type,
                api_function=name,
                traceback=render_failure(exc, redact=_as_is, with_traceback=True),
                **run_fields,
            )
            raise UnexpectedError(cause_type, api_function=name) from exc

    setattr(wrapper, BOUNDARY_MARK, True)
    return wrapper
