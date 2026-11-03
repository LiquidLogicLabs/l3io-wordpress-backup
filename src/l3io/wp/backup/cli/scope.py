"""FR-2/AD-22 — the one translation from the CLI's ``--mode`` to a Backup Scope.

The frozen CLI accepts ``files``, ``db`` and ``all``; the domain's values are
the Glossary's ``files``, ``database`` and ``all``. This table is the only
place ``db`` becomes ``database``. It is total over the parser's ``choices``
and has no fallback: a value outside it is a ``KeyError``, never another scope.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from l3io.wp.backup.domain.backup_scope import BackupScope

__all__ = ["CLI_MODE_TO_SCOPE", "backup_scope_from_cli"]

#: ``wpbackup2 backup --mode`` value -> Backup Scope.
CLI_MODE_TO_SCOPE: Final[Mapping[str, BackupScope]] = MappingProxyType(
    {
        "files": BackupScope.FILES,
        "db": BackupScope.DATABASE,
        "all": BackupScope.ALL,
    }
)


def backup_scope_from_cli(value: str) -> BackupScope:
    """The Backup Scope ``--mode value`` names. Raises ``KeyError`` otherwise."""
    return CLI_MODE_TO_SCOPE[value]
