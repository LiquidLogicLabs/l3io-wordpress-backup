"""FR-1 — the Backup Scope: what a backup captures.

Only ``all`` exists so far; E002-S01-002 adds the ``files`` and ``db`` members.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["BackupScope"]


class BackupScope(StrEnum):
    """What a backup captures."""

    ALL = "all"
