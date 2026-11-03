"""ADR-0016/AD-4 — the constructors of an ``AdapterSet``, in the composition-root layer.

:func:`real_adapters` binds the real adapters. :func:`dry_run_adapters` is the
only other constructor: a Dry Run is adapter selection, never a conditional
(AD-4), so it reuses the **same** query instances and tracer as the real set
and substitutes only the command halves with their no-ops from
``adapters/noop.py``. Later stories extend both for the fields they add
(E002-S02-012).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from l3io.wp.backup.adapters.database import WpDatabase
from l3io.wp.backup.adapters.noop import (
    ActionLog,
    NoopArchiveStore,
    NoopArchiveWriter,
    NoopDatabase,
)
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.adapters.store import LocalArchiveStore
from l3io.wp.backup.adapters.tar import (
    TarArchiveProbe,
    TarArchiveWriter,
    verify_or_raise,
)
from l3io.wp.backup.ports.adapter_set import AdapterSet
from l3io.wp.backup.ports.tracing import Tracer

if TYPE_CHECKING:
    from l3io.wp.database import CredentialSource

__all__ = ["dry_run_adapters", "real_adapters"]


def real_adapters(
    *,
    wordpress_source: CredentialSource,
    secrets: SecretRegistry,
    tracer: Tracer,
) -> AdapterSet:
    """One store for both store halves, one ``WpDatabase`` for both database halves."""
    store = LocalArchiveStore(verify=verify_or_raise)
    database = WpDatabase(wordpress_source, secrets=secrets)
    return AdapterSet(
        archive_store_query=store,
        archive_store_command=store,
        archive_probe=TarArchiveProbe(),
        database_query=database,
        database_command=database,
        archive_writer=TarArchiveWriter(),
        tracer=tracer,
    )


def dry_run_adapters(real: AdapterSet, *, log: ActionLog | None = None) -> AdapterSet:
    """``real`` with every command half replaced by its no-op (AD-4).

    Every other field — each query half, the probe and the tracer — is the
    very instance ``real`` holds. ``log`` receives the skipped calls; a fresh
    one when ``None``. Every field is named, not copied with
    ``dataclasses.replace``, so a field added to ``AdapterSet`` without a
    decision here fails mypy.
    """
    skipped = ActionLog() if log is None else log
    return AdapterSet(
        archive_store_query=real.archive_store_query,
        archive_store_command=NoopArchiveStore(skipped),
        archive_probe=real.archive_probe,
        database_query=real.database_query,
        database_command=NoopDatabase(skipped),
        archive_writer=NoopArchiveWriter(skipped),
        tracer=real.tracer,
    )
