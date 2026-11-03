"""ADR-0016 — the constructors of an ``AdapterSet``, in the composition-root layer.

:func:`real_adapters` is the only constructor until E002-S02-007 adds
``dry_run_adapters(...)``. Later stories extend its keyword arguments.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from l3io.wp.backup.adapters.database import WpDatabase
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.adapters.store import LocalArchiveStore
from l3io.wp.backup.adapters.tar import TarArchiveProbe, verify_or_raise
from l3io.wp.backup.ports.adapter_set import AdapterSet
from l3io.wp.backup.ports.tracing import Tracer

if TYPE_CHECKING:
    from l3io.wp.database import CredentialSource

__all__ = ["real_adapters"]


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
        tracer=tracer,
    )
