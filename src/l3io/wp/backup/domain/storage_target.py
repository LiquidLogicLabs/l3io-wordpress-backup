"""AG-27 — the Storage Target grammar.

The caller splits the operator's text with stdlib ``urllib.parse.urlsplit`` in
``cli/`` (a bare path has ``scheme=""``) and hands the two parts here, so
``domain/`` imports no ``urllib`` and nothing splits a URL by hand. Only
``file:`` targets are accepted in v1; remote backends are v1.1.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Literal

from l3io.wp.backup.domain.errors import UnsupportedStorageSchemeError, UsageError

__all__ = ["StorageTarget", "default_archive_name", "parse_storage_target"]

_FILE_SCHEMES = frozenset({"", "file"})


@dataclass(frozen=True, slots=True)
class StorageTarget:
    """Where an Archive is written."""

    scheme: Literal["file"]
    path: PurePosixPath


def parse_storage_target(scheme: str, path: str) -> StorageTarget:
    """Build a target from ``urlsplit``'s scheme and path parts."""
    if scheme.lower() not in _FILE_SCHEMES:
        raise UnsupportedStorageSchemeError(scheme=scheme)
    return StorageTarget(scheme="file", path=PurePosixPath(path))


def default_archive_name(db_name: str, now: datetime) -> str:
    """The legacy ``wpbackup2-<db_name>-<YYYYmmddHHMMSS>.tar.gz``, in UTC.

    ``now`` is supplied by the caller (``domain/`` reads no clock) and must be
    timezone-aware, so the UTC conversion never guesses the local zone.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        msg = "default_archive_name needs a timezone-aware datetime"
        raise UsageError(msg, arguments=("now",))
    stamp = now.astimezone(UTC).strftime("%Y%m%d%H%M%S")
    return f"wpbackup2-{db_name}-{stamp}.tar.gz"
