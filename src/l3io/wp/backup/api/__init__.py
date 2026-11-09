"""Importable composition root (AD-16, AD-22, FR-22).

``backup``, ``verify`` and ``restore`` drive the same composition sequence as
``wpbackup2``: each opens the Run (AD-16) through the driver the CLI calls, and
returns a frozen value or raises a package exception. Nothing here configures
logging: a host that wants this package's renderer calls ``configure_logging``
itself.

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

**The public surface (FR-24).** A name is public if and only if it is in
``l3io.wp.backup.__all__`` (``__version__``) or in this module's ``__all__``;
each has a heading in ``docs/api.md``, and each class and callable a
docstring. A re-exported class's
defining module is not part of the contract: import it from here. Every
module under the internal layers (``domain/``, ``ports/``, ``core/``,
``adapters/``, ``cli/``) is internal whatever its name, and so are
``api/operations.py`` and ``api/errors.py``, whose public names are the ones
re-exported here. Before v1.0 the surface is unstable (PRD §10); from v1.0 a
change to a listed name is a version event. ``cli/`` never imports this
package (import-linter's ``FR-24`` contract in ``pyproject.toml``).
``tests/contracts/test_public_surface.py`` and
``tests/contracts/test_api_docs.py`` enforce the layer classification, the
docstrings and the headings; the instability is policy.
"""

from __future__ import annotations

from l3io.wp.backup.adapters.preflight import Diagnosis
from l3io.wp.backup.adapters.tracing import add_trace_ids
from l3io.wp.backup.api import errors as _errors
from l3io.wp.backup.api.errors import *  # noqa: F403 - AD-14: the exception set
from l3io.wp.backup.api.operations import backup, doctor, restore, verify
from l3io.wp.backup.cli.backup import BackupResult
from l3io.wp.backup.cli.logging import configure_logging
from l3io.wp.backup.cli.output import RunWarning
from l3io.wp.backup.cli.restore_request import RelocationFlags, RestoreOutcome
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
from l3io.wp.backup.domain.secret import Secret
from l3io.wp.backup.domain.site import SiteDescriptor
from l3io.wp.backup.domain.storage_target import StorageTarget
from l3io.wp.backup.ports.database import DbTls
from l3io.wp.backup.ports.exclusion import ExclusionPolicy

__all__: list[str] = [
    "AwsSecretCredentialSpec",
    "BackupResult",
    "BackupScope",
    "ConfigPolicy",
    "CredentialSpec",
    "DbTls",
    "Diagnosis",
    "ExclusionPolicy",
    "ExclusionSet",
    "ExplicitCredentialSpec",
    "Override",
    "OverrideSet",
    "RelocationFlags",
    "RestoreMode",
    "RestoreOutcome",
    "RunWarning",
    "Secret",
    "SiteDescriptor",
    "SnapshotPolicy",
    "StorageTarget",
    "VerificationResult",
    "add_trace_ids",
    "backup",
    "configure_logging",
    "doctor",
    "enumerate_defaults",
    "restore",
    "verify",
]
__all__ += _errors.__all__
# ``RunContext`` is not public (owner decision 2026-11-07, BL-E008-028): no
# entry point accepts or returns one. It returns with ``open_run()`` post-v1
# (BL-E006-025).
