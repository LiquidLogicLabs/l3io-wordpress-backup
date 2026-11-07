"""Importable composition root (AD-16, AD-22, FR-22).

``backup``, ``verify`` and ``restore`` drive the same composition sequence as
``wpbackup2``: each opens the Run (AD-16) — or joins the ``RunContext`` it is
handed and creates nothing — through the driver the CLI calls, and returns a
frozen value or raises a package exception. Nothing here configures logging:
a host that wants this package's renderer calls ``configure_logging`` itself.

Every other name is re-exported by identity, never redefined: the values the
entry points take and return, ``enumerate_defaults`` (the library's
``backup --list-default-excludes``), which opens no Run, and ``add_trace_ids``,
the structlog processor that puts flat ``trace_id``/``span_id`` on records
while a span is recording (AD-24): pass it as
``configure_logging(level, fmt, trace_processors=(add_trace_ids,))``, or add
it to a chain the host configures itself.

**Errors (AD-14).** Every exception class the package raises is exported here
from ``api/errors.py``: ``except BackupError`` catches them all, and each
subclass carries typed fields and its ``exit_code``. A non-package exception
reaching an entry point arrives as ``UnexpectedError`` with the original on
``__cause__``.
"""

from __future__ import annotations

from l3io.wp.backup.adapters.database import AdminTls
from l3io.wp.backup.adapters.tracing import add_trace_ids
from l3io.wp.backup.api import errors as _errors
from l3io.wp.backup.api.errors import *  # noqa: F403 - AD-14: the exception set
from l3io.wp.backup.api.operations import backup, restore, verify
from l3io.wp.backup.cli.backup import BackupResult
from l3io.wp.backup.cli.context import RunContext
from l3io.wp.backup.cli.logging import configure_logging
from l3io.wp.backup.cli.restore import RelocationFlags, RestoreOutcome
from l3io.wp.backup.core.exclusions import ExclusionSet, enumerate_defaults
from l3io.wp.backup.domain.archive_format import VerificationResult
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.credentials import (
    AwsSecretCredentialSpec,
    CredentialSpec,
    ExplicitCredentialSpec,
)
from l3io.wp.backup.domain.restore import (
    ConfigPolicy,
    Override,
    OverrideSet,
    RestoreMode,
    SnapshotPolicy,
)
from l3io.wp.backup.domain.site import SiteDescriptor
from l3io.wp.backup.domain.storage_target import StorageTarget
from l3io.wp.backup.ports.exclusion import ExclusionPolicy

__all__: list[str] = [
    "AdminTls",
    "AwsSecretCredentialSpec",
    "BackupResult",
    "BackupScope",
    "ConfigPolicy",
    "CredentialSpec",
    "ExclusionPolicy",
    "ExclusionSet",
    "ExplicitCredentialSpec",
    "Override",
    "OverrideSet",
    "RelocationFlags",
    "RestoreMode",
    "RestoreOutcome",
    "RunContext",
    "SiteDescriptor",
    "SnapshotPolicy",
    "StorageTarget",
    "VerificationResult",
    "add_trace_ids",
    "backup",
    "configure_logging",
    "enumerate_defaults",
    "restore",
    "verify",
]
__all__ += _errors.__all__
