"""AD-4/AD-21 — the Archive store port, split into a query and a command.

No method moves Archive bytes as ``bytes``: a staged Archive is written through
a file object (AD-21, AG-13). A Dry Run binds the real query and a no-op
command.

``ArchiveProbe`` (AD-23, AG-7) is a query port over an Archive's bytes: raw
facts only, it never decides a format (``domain.archive_format.classify``
does). Its one ``bytes`` return is :meth:`ArchiveProbe.read_member`, bounded by
``max_bytes`` and exempted by name in ``tests/contracts/test_port_types.py``
(ADR-0016 amendment).

``ArchiveWriterCommand`` (ADR-0019) is where the Site Root is collected, the
payload encrypted and the Archive written, so a Dry Run, binding its no-op,
reads no Site Root file and writes nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import IO, Protocol, runtime_checkable

from l3io.wp.backup.domain.archive_format import (
    ArchiveView,
    Manifest,
    ManifestMember,
    MemberDigest,
    MemberIndex,
)
from l3io.wp.backup.domain.exclusion import ExcludedEntry
from l3io.wp.backup.domain.storage_target import StorageTarget
from l3io.wp.backup.ports.crypto import PayloadCipher
from l3io.wp.backup.ports.exclusion import ExclusionPolicy

__all__ = [
    "ArchiveProbe",
    "ArchiveStoreCommand",
    "ArchiveStoreQuery",
    "ArchiveWriterCommand",
    "ManifestBuilder",
    "StagedArchive",
]

#: Builds the Manifest from the member index written and what was excluded.
ManifestBuilder = Callable[
    [tuple[ManifestMember, ...], tuple[ExcludedEntry, ...]], Manifest
]


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


@runtime_checkable
class ArchiveStoreCommand(Protocol):
    """What changes the Archive store."""

    def stage(self, target: StorageTarget) -> StagedArchive:
        """Begin writing an Archive for ``target``."""
        ...


@runtime_checkable
class ArchiveWriterCommand(Protocol):
    """What collects a Site and writes its Archive (ADR-0019)."""

    def write(
        self,
        out: IO[bytes],
        *,
        site_root: Path | None,
        dump: Path | None,
        config: Path,
        exclusion: ExclusionPolicy,
        cipher: PayloadCipher | None,
        manifest_builder: ManifestBuilder,
        workspace: Path,
    ) -> Manifest:
        """Write one Archive into ``out``; its Manifest.

        ``site_root`` is ``None`` for a ``database`` scope and ``dump`` for
        ``files``. ``workspace`` is the Run's private directory, where the
        payload is staged before it is copied into ``out``.
        """
        ...


class ArchiveProbe(Protocol):
    """What can be read from an Archive without writing anything."""

    def member_index(self, source: StorageTarget) -> MemberIndex:
        """The outer tar's member names and Manifest text, streamed once."""
        ...

    def member_digests(self, source: StorageTarget) -> Mapping[str, MemberDigest]:
        """Every outer member's type, size and SHA-256, streamed once."""
        ...

    def read_member(
        self,
        source: StorageTarget,
        view: ArchiveView,
        path: str,
        *,
        max_bytes: int,
        payload: Path | None = None,
    ) -> bytes:
        """One regular-file member's bytes, at most ``max_bytes`` of them."""
        ...
