"""FR-23, AD-7, ADR-0018 item 3 — no exception reaching a library caller holds a secret.

Each failure mode is driven through ``l3io.wp.backup.api`` with doubles,
the real adapters in place wherever the double stands in for the server.
The secrets are planted as the database password (``wp-config.php``), the
administrator password (``admin=``) and the passphrase (``passphrase=`` and
``L3IO_WP_BACKUP_PASSPHRASE``). Four surfaces a host might log are searched,
each taken **as is** with no redaction: ``str``, ``repr``, every structured
attribute's ``repr``, and ``traceback.format_exception`` (the ``__cause__``
chain included).

A second check covers the outward paths: for **every** exported class, built
with a secret in every field it takes, ``render_failure`` (with its
traceback) and ``redact_event`` over a record carrying the exception and its
``exc_info`` (AD-7's redaction) leave no secret in what they emit. Fields
reach those paths through the message and the traceback, never on their own.
"""

from __future__ import annotations

import gzip
import inspect
import io
import tarfile
import traceback
import typing
from collections.abc import Callable
from pathlib import Path, PurePath, PurePosixPath
from types import UnionType
from typing import Any, Literal

import pytest
from structlog.testing import capture_logs

import l3io.wp.backup.api as api
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.cli.exit import render_failure
from l3io.wp.backup.domain.archive_format import (
    Integrity,
    VerificationFailure,
    VerificationReason,
    VerificationResult,
)
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.database import DatabaseLocation
from l3io.wp.backup.domain.errors import (
    BackupError,
    CredentialsError,
    DatabaseRefusedError,
    DatabaseUnreachableError,
    MissingExtraError,
    PassphraseRejectedError,
    SiteLockedError,
    VerificationFailedError,
)
from l3io.wp.backup.domain.restore import (
    ConfigPolicy,
    OverrideSet,
    RestoreMode,
    SnapshotPolicy,
)
from tests.conftest import CredentialSentinel, seal_archive
from tests.exception_scope import exported_exceptions
from tests.root_mode import allow_root_kwargs
from tests.site_lock_holder import held_elsewhere

wpdb = pytest.importorskip("l3io.wp.database")

PASSPHRASE = pytest.mark.parametrize(
    "credential_sentinel", [{"passphrase": True}], indirect=True
)


def surfaces(exc: BaseException) -> dict[str, str]:
    """What a host might log, each exactly as Python renders it."""
    found = {"str": str(exc), "repr": repr(exc)}
    found |= {f"attr {k}": repr(v) for k, v in vars(exc).items()}
    found["traceback"] = "".join(traceback.format_exception(exc))
    return found


def _leaks(exc: BaseException, secrets: tuple[str, ...]) -> list[str]:
    return sorted(
        name
        for name, text in surfaces(exc).items()
        if any(secret in text for secret in secrets)
    )


def _backup(sentinel: CredentialSentinel, tmp_path: Path) -> None:
    api.backup(
        sentinel.site,
        scope=BackupScope.DATABASE,
        target=tmp_path / "out.tar.gz",
        encrypt=True,
        passphrase=sentinel.passphrase,
    )


def _check_dump_raises(
    monkeypatch: pytest.MonkeyPatch, make: Callable[[], BaseException]
) -> None:
    def check_dump(*args: object, **kwargs: object) -> object:
        raise make()

    monkeypatch.setattr(wpdb, "check_dump", check_dump)


MODES: dict[str, tuple[Callable[[], BaseException], type[BaseException]]] = {
    "credentials": (lambda: wpdb.AuthenticationFailedError("denied"), CredentialsError),
    "unreachable": (
        lambda: wpdb.DatabaseUnreachableError("down"),
        DatabaseUnreachableError,
    ),
    "refused": (lambda: wpdb.AccessDeniedError("no privilege"), DatabaseRefusedError),
    "missing extra": (
        lambda: wpdb.MissingDependencyError("database", "l3io-wordpress-database"),
        MissingExtraError,
    ),
    "unexpected": (lambda: RuntimeError("a defect"), api.UnexpectedError),
}


@PASSPHRASE
@pytest.mark.parametrize("mode", sorted(MODES))
def test_a_database_failure_through_the_api_carries_no_secret(
    mode: str,
    credential_sentinel: CredentialSentinel,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    make, expected = MODES[mode]
    _check_dump_raises(monkeypatch, make)
    with pytest.raises(BackupError) as caught:
        _backup(credential_sentinel, tmp_path)
    assert type(caught.value) is expected
    assert _leaks(caught.value, credential_sentinel.secrets) == []


@PASSPHRASE
def test_a_held_lock_through_the_api_carries_no_secret(
    credential_sentinel: CredentialSentinel, tmp_path: Path
) -> None:
    with (
        held_elsewhere(credential_sentinel.site),
        pytest.raises(SiteLockedError) as caught,
    ):
        _backup(credential_sentinel, tmp_path)
    assert caught.value.holder_run_id is not None
    assert _leaks(caught.value, credential_sentinel.secrets) == []


def _damage_a_member(archive: Path) -> Path:
    """``archive`` with one byte of its payload member's data flipped.

    The tar (and gzip, when there is one) layer stays readable, so the scan
    reaches the payload and reports a checksum mismatch rather than an
    unreadable Archive.
    """
    data = archive.read_bytes()
    zipped = data[:2] == b"\x1f\x8b"
    raw = bytearray(gzip.decompress(data) if zipped else data)
    with tarfile.open(fileobj=io.BytesIO(bytes(raw))) as tar:
        [payload] = [m for m in tar.getmembers() if m.isfile() and "payload" in m.name]
    raw[payload.offset_data + 1] ^= 0xFF
    damaged = archive.with_name("damaged.tar.gz")
    damaged.write_bytes(gzip.compress(bytes(raw)) if zipped else bytes(raw))
    return damaged


@PASSPHRASE
def test_a_failed_verification_through_the_api_carries_no_secret(
    credential_sentinel: CredentialSentinel,
) -> None:
    archive = seal_archive(credential_sentinel.workdir, credential_sentinel.passphrase)
    damaged = _damage_a_member(archive)
    with capture_logs(), pytest.raises(VerificationFailedError) as caught:
        api.verify(
            damaged, authenticate=True, passphrase=credential_sentinel.passphrase
        )
    assert caught.value.result.failures
    assert _leaks(caught.value, credential_sentinel.secrets) == []


@PASSPHRASE
def test_a_rejected_passphrase_through_the_api_carries_no_secret(
    credential_sentinel: CredentialSentinel,
) -> None:
    archive = seal_archive(credential_sentinel.workdir, credential_sentinel.passphrase)
    wrong = credential_sentinel.relocated_password
    with capture_logs(), pytest.raises(PassphraseRejectedError) as caught:
        api.verify(archive, authenticate=True, passphrase=wrong)
    assert caught.value.scheme == "age"
    assert _leaks(caught.value, credential_sentinel.secrets) == []


#: Every ``l3io.wp.database`` operation the real adapter can call.
DATABASE_OPERATIONS = (
    "check_drop_database",
    "check_dump",
    "check_ensure",
    "check_load",
    "check_revoke",
    "drop_database",
    "dump",
    "ensure",
    "load",
    "revoke",
    "survey",
)


@PASSPHRASE
def test_a_rejected_administrator_through_the_api_carries_no_secret(
    credential_sentinel: CredentialSentinel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A database restore with ``admin=`` through the real adapters: the server
    double rejects the administrator, so the admin path runs and its mapped
    failure is what reaches the caller."""
    called: list[str] = []
    for name in DATABASE_OPERATIONS:

        def reject(*args: object, _name: str = name, **kwargs: object) -> object:
            called.append(_name)
            text = "access denied"
            raise wpdb.AuthenticationFailedError(text)

        monkeypatch.setattr(wpdb, name, reject)
    archive = seal_archive(credential_sentinel.workdir, credential_sentinel.passphrase)
    with capture_logs(), pytest.raises(CredentialsError) as caught:
        api.restore(
            credential_sentinel.site,
            archive=archive,
            mode=RestoreMode.DATABASE,
            overrides=OverrideSet.none(),
            snapshot=SnapshotPolicy.SKIP,
            config_policy=ConfigPolicy.KEEP,
            admin=api.ExplicitCredentialSpec(
                username="root", password=credential_sentinel.admin_password
            ),
            passphrase=credential_sentinel.passphrase,
            **allow_root_kwargs(),
        )
    assert called, "no database operation ran: the admin path was not reached"
    assert "explicit" in caught.value.source
    assert isinstance(caught.value.__cause__, wpdb.AuthenticationFailedError)
    assert _leaks(caught.value, credential_sentinel.secrets) == []


def test_the_surface_search_finds_a_planted_secret() -> None:
    """Planted: a secret in a field, in a cause's text and in a message."""
    secret = "planted-secret-value"
    in_field = CredentialsError("wp-config")
    in_field.planted = secret  # type: ignore[attr-defined]
    in_cause = CredentialsError("wp-config")
    in_cause.__cause__ = ValueError(secret)
    assert _leaks(in_field, (secret,)) == ["attr planted"]
    assert _leaks(in_cause, (secret,)) == ["traceback"]
    assert _leaks(CredentialsError(secret), (secret,)) == [
        "attr source",
        "repr",
        "str",
        "traceback",
    ]


# --------------------------------------------------------------------------
# AD-7's redaction covers every field of every exported class
# --------------------------------------------------------------------------

SECRET = "field-secret-0123456789"


def _value(annotation: Any) -> object:  # noqa: ANN401
    """A value of ``annotation`` that carries :data:`SECRET` wherever it can."""
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if annotation is str:
        return SECRET
    if annotation in (PurePath, Path, PurePosixPath):
        return annotation("/srv", SECRET)
    if annotation is int:
        return 7
    if annotation is bool:
        return True
    if origin is Literal:
        return args[0]
    if origin in (UnionType, typing.Union):
        usable = [a for a in args if a is not type(None)]
        return _value(str if str in usable else usable[0])
    if origin is tuple:
        return (
            (_value(args[0]),)
            if len(args) == 2 and args[1] is Ellipsis
            else tuple(_value(a) for a in args)
        )
    if annotation is object:
        return SECRET
    if annotation is BaseException:
        return ValueError(SECRET)
    if annotation is VerificationResult:
        return VerificationResult(
            passed=False,
            integrity=Integrity.STRUCTURAL_ONLY,
            format_version=1,
            failures=(
                VerificationFailure(
                    member=SECRET, reason=VerificationReason.CHECKSUM_MISMATCH
                ),
            ),
        )
    if annotation is DatabaseLocation:
        return DatabaseLocation(host=SECRET, port=3306, name=SECRET)
    if isinstance(annotation, type) and issubclass(annotation, BaseException):
        return _build(annotation)
    if isinstance(annotation, type) and hasattr(annotation, "__members__"):
        return next(iter(annotation.__members__.values()))
    msg = f"no value for {annotation!r}: teach _value this type"
    raise AssertionError(msg)


def _build(cls: type[BaseException]) -> BaseException:
    hints = typing.get_type_hints(cls.__init__, globalns=_namespace(cls))
    kwargs = {}
    positional = []
    for name, parameter in inspect.signature(cls.__init__).parameters.items():
        if name == "self" or parameter.kind in (
            parameter.VAR_POSITIONAL,
            parameter.VAR_KEYWORD,
        ):
            continue
        value = _value(hints[name])
        if parameter.kind is parameter.KEYWORD_ONLY:
            kwargs[name] = value
        else:
            positional.append(value)
    return cls(*positional, **kwargs)


def _namespace(cls: type) -> dict[str, Any]:
    import sys

    namespace: dict[str, Any] = {}
    for klass in cls.__mro__:
        namespace |= vars(sys.modules[klass.__module__])
    namespace |= {
        "VerificationResult": VerificationResult,
        "DatabaseLocation": DatabaseLocation,
    }
    return namespace


BUILDABLE = {
    name: cls
    for name, cls in exported_exceptions(api.__all__, api).items()
    if cls.__init__ not in (BaseException.__init__, Exception.__init__)  # type: ignore[comparison-overlap]
}


def test_every_exported_class_with_fields_is_built() -> None:
    assert len(BUILDABLE) > 50


@pytest.mark.parametrize("name", sorted(BUILDABLE))
def test_redaction_covers_every_field(name: str) -> None:
    exc = _build(BUILDABLE[name])
    registry = SecretRegistry()
    registry.register(SECRET, source="test")
    rendered = render_failure(exc, redact=registry.redact, with_traceback=True)
    assert SECRET not in rendered
    event: dict[str, Any] = {"event": "command_failed", "error": exc, "exc_info": exc}
    redacted = registry.redact_event(None, "error", event)
    assert SECRET not in repr(redacted)


def test_without_redaction_a_built_field_secret_is_seen() -> None:
    """Planted: the same build, redaction removed, must show the secret, so the
    check above is not passing over messages that never held it."""
    shown = [
        name
        for name, cls in sorted(BUILDABLE.items())
        if SECRET in render_failure(_build(cls), redact=lambda s: s)
    ]
    assert len(shown) > len(BUILDABLE) // 2
    assert "CredentialsError" in shown
