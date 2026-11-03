"""FR-3 — ``wpbackup2 verify --archive <target>``: check an Archive, touch no Site.

:func:`drive_verify` opens or joins a Run (AD-16), binds nothing, and calls
:func:`~l3io.wp.backup.adapters.tar.verify_archive`: verification reads the
Archive and writes nothing, and is keyless (ADR-0024), so
:class:`VerifyRequest` carries no cipher. ``0`` on a pass,
``1`` on a fail or an error (E002-S01-005 maps the fail to
``VERIFICATION_FAILED``). The output names ``integrity``; a v0 pass says that
integrity was not checked, because a v0 Archive carries no checksums.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.adapters.tar import verify_archive
from l3io.wp.backup.cli.backup import storage_target_from_cli
from l3io.wp.backup.cli.context import RunContext, join_or_open
from l3io.wp.backup.cli.output import emit_failure, emit_result
from l3io.wp.backup.domain.archive_format import (
    Integrity,
    VerificationResult,
    format_label,
)
from l3io.wp.backup.domain.errors import BackupError, VerificationFailedError
from l3io.wp.backup.domain.storage_target import StorageTarget
from l3io.wp.backup.ports.tracing import Tracer

__all__ = ["VerifyRequest", "drive_verify", "run_verify"]


@dataclass(frozen=True, slots=True)
class VerifyRequest:
    """The Archive to check. No cipher: verification is keyless (ADR-0024)."""

    archive: StorageTarget


def _describe(result: VerificationResult) -> str:
    version = format_label(result.format_version)
    label = version if isinstance(version, str) else f"v{version}"
    if result.integrity is Integrity.STRUCTURAL_ONLY:
        return (
            f"integrity {result.integrity.value} (format {label}): integrity was "
            "not checked — a v0 archive carries no checksums"
        )
    return f"integrity {result.integrity.value} (format {label})"


def drive_verify(
    request: VerifyRequest,
    *,
    ctx: RunContext | None,
    tracer: Tracer | None = None,
    secrets: SecretRegistry | None = None,
    command_args: Sequence[str] | None = None,
) -> VerificationResult:
    """The verify driver: returns or raises ``BackupError``; never prints.

    A failed verification is returned, not raised; a Run opened here still
    finishes with a non-zero ``exit_code`` (``report_failure``).
    """
    with join_or_open(
        ctx,
        operation="verify",
        tracer=tracer,
        secrets=secrets,
        command_args=command_args,
    ) as handle:
        result = verify_archive(Path(request.archive.path))
        if handle.opened and not result.passed:
            handle.ctx.report_failure(
                exit_code=1, error_type=VerificationFailedError.__name__
            )
        return result


def run_verify(args: argparse.Namespace) -> int:
    """``wpbackup2 verify``: ``0`` on a pass, non-zero on a fail or an error.

    Verify resolves no credential, but its Run still owns a ``SecretRegistry``
    and renders its error through it (ADR-0018), so the boundary is the same
    for every command.
    """
    secrets = SecretRegistry()
    try:
        target = storage_target_from_cli(args.archive)
        result = drive_verify(VerifyRequest(archive=target), ctx=None, secrets=secrets)
    except BackupError as exc:
        emit_failure(
            "command_failed",
            f"wpbackup2: error: {exc}",
            error=type(exc).__name__,
            secrets=secrets,
        )
        return 1
    path = target.path
    if result.passed:
        emit_result(f"{path}: OK, {_describe(result)}")
        return 0
    first = result.failures[0]
    member = "" if first.member is None else f" ({first.member!r})"
    emit_failure(
        "verify_failed",
        f"wpbackup2: verify failed: {path}: {first.reason.value}{member}",
        error=VerificationFailedError.__name__,
        secrets=secrets,
    )
    return 1
