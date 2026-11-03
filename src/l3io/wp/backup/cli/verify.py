"""FR-3 — ``wpbackup2 verify --archive <target> [--authenticate]``: touch no Site.

:func:`drive_verify` opens or joins a Run (AD-16), binds nothing, and calls
:func:`~l3io.wp.backup.adapters.tar.verify_archive_view` inside one ``VERIFY``
phase: verification reads the Archive and writes nothing. ``ExitCode.OK`` on a
pass; a fail raises
:class:`~l3io.wp.backup.domain.errors.VerificationFailedError`, which the root
maps to ``VERIFICATION_FAILED`` (FR-21).

**What a pass proves (BL-E002-060, Shawn 2026-11-03).** Keyless verification
(ADR-0024) checks the payload against the sha256 in the Manifest, which nothing
protects: whoever rewrites the payload can rewrite that checksum too. A keyless
v1 pass therefore proves the checksums are **consistent**, not that the
Archive is **authentic**; the output says so and the ``VERIFY`` record carries
``authenticated: false``. ``Integrity.VERIFIED`` keeps its name. A v0 pass
says integrity was not checked, because a v0 Archive carries no checksums.

**``--authenticate``** (``VerifyRequest.authenticate``) is a boolean; no flag
takes a passphrase (FR-4, NFR-3). After a keyless pass it resolves the
passphrase through the AD-26 chain (:func:`resolve_passphrase`, registered
with the Run's ``SecretRegistry``) and decrypts the whole payload with
:func:`~l3io.wp.backup.adapters.tar.decrypt_payload` into the Run's workspace;
``age`` authenticates every chunk, so a pass is authentic and the record
carries ``authenticated: true``. A wrong passphrase is
``PassphraseRejectedError`` and a damaged chunk ``PayloadDamagedError``, both
exit 8. An Archive with no encrypted payload is refused
(``ArchiveNotEncryptedError``, a usage error): there is nothing to
authenticate. No passphrase resolved is ``PassphraseRequiredError``.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final

from l3io.wp.backup.adapters.crypto import PassphraseCipher
from l3io.wp.backup.adapters.phase import phase
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.adapters.tar import decrypt_payload, verify_archive_view
from l3io.wp.backup.cli.backup import storage_target_from_cli
from l3io.wp.backup.cli.context import RunContext, join_or_open
from l3io.wp.backup.cli.output import emit_result
from l3io.wp.backup.cli.passphrase import resolve_passphrase, terminal_prompt
from l3io.wp.backup.core.backup import BackupStep
from l3io.wp.backup.domain.archive_format import (
    ArchiveView,
    Integrity,
    VerificationResult,
    format_label,
)
from l3io.wp.backup.domain.errors import (
    ArchiveNotEncryptedError,
    PassphraseRequiredError,
    VerificationFailedError,
)
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.domain.storage_target import StorageTarget
from l3io.wp.backup.ports.tracing import Tracer

__all__ = ["VerifyRequest", "drive_verify", "run_verify"]

_NO_ENVIRONMENT: Final[Mapping[str, str]] = MappingProxyType({})
#: The decrypted payload's name in the workspace; deleted once ``age`` accepts it.
_AUTHENTICATED_PAYLOAD: Final = "authenticate.payload"


@dataclass(frozen=True, slots=True)
class VerifyRequest:
    """The Archive to check, and whether to authenticate it with its passphrase.

    Keyless by default (ADR-0024). It carries no passphrase and no cipher:
    ``authenticate=True`` resolves one through the AD-26 chain.
    """

    archive: StorageTarget
    authenticate: bool = False


def _describe(result: VerificationResult, *, authenticated: bool) -> str:
    version = format_label(result.format_version)
    label = version if isinstance(version, str) else f"v{version}"
    head = f"integrity {result.integrity.value} (format {label})"
    if result.integrity is Integrity.STRUCTURAL_ONLY:
        return f"{head}: integrity was not checked — a v0 archive carries no checksums"
    if authenticated:
        return f"{head}: checksums consistent — authenticated with the passphrase"
    return f"{head}: checksums consistent — authenticity not checked (no key)"


def _authenticate(
    target: StorageTarget,
    view: ArchiveView | None,
    *,
    ctx: RunContext,
    passphrase: str | None,
    environ: Mapping[str, str],
    prompt: Callable[[str], str] | None,
) -> None:
    """Decrypt the whole payload into the workspace; raise unless ``age`` accepts it."""
    if view is None or not view.encrypted:
        raise ArchiveNotEncryptedError(str(target.path))
    resolved = resolve_passphrase(
        explicit=passphrase, environ=environ, prompt=prompt, secrets=ctx.secrets
    )
    if resolved is None:
        operation: Final = "verify"
        raise PassphraseRequiredError(operation)
    cipher = PassphraseCipher(resolved.value)
    cipher.check()
    plain = decrypt_payload(
        target, view, cipher=cipher, destination=ctx.workspace / _AUTHENTICATED_PAYLOAD
    )
    # The plaintext is not needed; the workspace removes it anyway (AD-12).
    plain.unlink(missing_ok=True)


def drive_verify(
    request: VerifyRequest,
    *,
    ctx: RunContext | None,
    tracer: Tracer | None = None,
    secrets: SecretRegistry | None = None,
    command_args: Sequence[str] | None = None,
    passphrase: str | None = None,
    environ: Mapping[str, str] = _NO_ENVIRONMENT,
    prompt: Callable[[str], str] | None = None,
) -> VerificationResult:
    """The verify driver: returns or raises ``BackupError``; never prints.

    A failed verification is returned, not raised; a Run opened here still
    finishes with a non-zero ``exit_code`` (``report_failure``). With
    ``request.authenticate`` a returned pass has also been authenticated;
    ``passphrase``, ``environ`` and ``prompt`` are AD-26's chain, in that
    order, and are read only then. A library caller's ``environ`` defaults to
    empty: nothing below a root reads ``os.environ``.
    """
    with join_or_open(
        ctx,
        operation="verify",
        tracer=tracer,
        secrets=secrets,
        command_args=command_args,
    ) as handle:
        with phase(BackupStep.VERIFY.value) as record:
            view, result = verify_archive_view(Path(request.archive.path))
            if request.authenticate and result.passed:
                _authenticate(
                    request.archive,
                    view,
                    ctx=handle.ctx,
                    passphrase=passphrase,
                    environ=environ,
                    prompt=prompt,
                )
                record.fields["authenticated"] = True
        if handle.opened and not result.passed:
            handle.ctx.report_failure(
                exit_code=VerificationFailedError.exit_code,
                error_type=VerificationFailedError.__name__,
            )
        return result


def run_verify(args: argparse.Namespace, *, secrets: SecretRegistry) -> ExitCode:
    """``wpbackup2 verify``: ``ExitCode.OK`` on a pass; raises on a fail or error.

    The Run owns the root's ``SecretRegistry``, and the root renders its error
    through it (ADR-0018), so the boundary is the same for every command. Only
    ``--authenticate`` resolves a credential: the passphrase, from
    ``os.environ`` or a terminal prompt — never from ``argv``.
    """
    target = storage_target_from_cli(args.archive)
    request = VerifyRequest(archive=target, authenticate=args.authenticate)
    result = drive_verify(
        request,
        ctx=None,
        secrets=secrets,
        environ=os.environ if request.authenticate else _NO_ENVIRONMENT,
        prompt=terminal_prompt() if request.authenticate else None,
    )
    if not result.passed:
        raise VerificationFailedError(result)
    emit_result(
        f"{target.path}: OK, {_describe(result, authenticated=args.authenticate)}"
    )
    return ExitCode.OK
