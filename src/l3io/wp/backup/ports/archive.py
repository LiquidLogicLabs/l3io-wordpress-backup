"""AD-4/AD-21 — the Archive store port, split into a query and a command.

No method moves Archive bytes as ``bytes``: a staged Archive is written through
a file object (AD-21, AG-13). A Dry Run binds the real query and a no-op
command. E002-S01-003 adds ``ArchiveProbe`` here.
"""

from __future__ import annotations

from typing import IO, Protocol

from l3io.wp.backup.domain.storage_target import StorageTarget

__all__ = ["ArchiveStoreCommand", "ArchiveStoreQuery", "StagedArchive"]


class StagedArchive(Protocol):
    """An Archive being written, invisible at its destination until published."""

    @property
    def stream(self) -> IO[bytes]:
        """Where the Archive's bytes are written."""
        ...

    def publish(self) -> bool:
        """Make the Archive visible; ``False`` only for the no-op store."""
        ...

    def discard(self) -> None:
        """Remove the staged bytes; nothing becomes visible."""
        ...


class ArchiveStoreQuery(Protocol):
    """What the Archive store can answer without changing anything."""

    def exists(self, target: StorageTarget) -> bool:
        """Whether an Archive already exists at ``target``."""
        ...


class ArchiveStoreCommand(Protocol):
    """What changes the Archive store."""

    def stage(self, target: StorageTarget) -> StagedArchive:
        """Begin writing an Archive for ``target``."""
        ...
