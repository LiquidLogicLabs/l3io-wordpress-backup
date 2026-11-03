"""FR-2 — the Backup Scope: what a backup captures.

The values are the PRD §4 Glossary's, verbatim: ``files``, ``database`` and
``all``. The CLI spells ``database`` as ``db`` (AD-22's frozen surface); that
translation lives in exactly one place, ``l3io.wp.backup.cli.scope``, and the
domain never spells it ``db``.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["BackupScope"]


class BackupScope(StrEnum):
    """What a backup captures."""

    FILES = "files"
    DATABASE = "database"
    ALL = "all"
