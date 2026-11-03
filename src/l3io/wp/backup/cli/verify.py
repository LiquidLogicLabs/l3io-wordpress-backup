"""FR-3 — ``wpbackup2 verify --archive <target>``: check an Archive, touch no Site.

Binds the real :func:`~l3io.wp.backup.adapters.tar.verify_archive` and opens no
workspace: verification reads the Archive and writes nothing. ``0`` on a pass,
``1`` on a fail or an error (E002-S01-005 maps the fail to
``VERIFICATION_FAILED``). The output names ``integrity``; a v0 pass says that
integrity was not checked, because a v0 Archive carries no checksums.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import structlog

from l3io.wp.backup.adapters.tar import verify_archive
from l3io.wp.backup.cli.backup import storage_target_from_cli
from l3io.wp.backup.domain.archive_format import (
    Integrity,
    VerificationResult,
    format_label,
)
from l3io.wp.backup.domain.errors import BackupError

__all__ = ["run_verify"]

_log = structlog.get_logger(__name__)


def _describe(result: VerificationResult) -> str:
    version = format_label(result.format_version)
    label = version if isinstance(version, str) else f"v{version}"
    if result.integrity is Integrity.STRUCTURAL_ONLY:
        return (
            f"integrity {result.integrity.value} (format {label}): integrity was "
            "not checked — a v0 archive carries no checksums"
        )
    return f"integrity {result.integrity.value} (format {label})"


def run_verify(args: argparse.Namespace) -> int:
    """``wpbackup2 verify``: ``0`` on a pass, non-zero on a fail or an error."""
    try:
        target = storage_target_from_cli(args.archive)
        result = verify_archive(Path(target.path))
    except BackupError as exc:
        _log.error("verify_failed", error=type(exc).__name__)
        print(f"wpbackup2: error: {exc}", file=sys.stderr)
        return 1
    if result.passed:
        print(f"{target.path}: OK, {_describe(result)}")
        return 0
    first = result.failures[0]
    member = "" if first.member is None else f" ({first.member!r})"
    print(
        f"wpbackup2: verify failed: {target.path}: {first.reason.value}{member}",
        file=sys.stderr,
    )
    return 1
