"""ADR-0016 — the one container of a Run's bound adapters.

Defined here and nowhere else (``tests/contracts/test_adapter_set_owner.py``).
Later stories **add fields** for the ports they introduce (E002-S02-007,
E002-S02-012); no story defines a second container. The cipher and the
exclusion policy are not fields: they are per-call arguments of ``backup(...)``.

``cli.adapter_sets.real_adapters`` is the real constructor.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from l3io.wp.backup.ports.archive import (
    ArchiveProbe,
    ArchiveStoreCommand,
    ArchiveStoreQuery,
)
from l3io.wp.backup.ports.database import DatabaseCommand, DatabaseQuery
from l3io.wp.backup.ports.tracing import Tracer

__all__ = ["AdapterSet"]


@dataclass(frozen=True, slots=True)
class AdapterSet:
    """Every port a Run reaches effects through, bound once (ADR-0016)."""

    archive_store_query: ArchiveStoreQuery
    archive_store_command: ArchiveStoreCommand
    archive_probe: ArchiveProbe
    database_query: DatabaseQuery
    database_command: DatabaseCommand
    tracer: Tracer

    def __repr__(self) -> str:
        # Types only: the database halves hold a credential source.
        fields = ", ".join(
            f"{f.name}={type(getattr(self, f.name)).__name__}"
            for f in dataclasses.fields(self)
        )
        return f"AdapterSet({fields})"
