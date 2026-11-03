"""AD-1 — the backup plan: which steps a backup runs, computed and never performed.

Pure. :func:`build_backup_plan` receives every value it needs as an argument —
it reads no path, clock or environment it was not handed (AD-12). ``scope``
and ``encrypted`` are required with no default, so no caller selects a
scope or skips encryption by omission. E002-S01-002 adds the per-scope plans;
E002-S02-007 adds ``required_tools`` and the active exclusion set.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.site import SiteDescriptor
from l3io.wp.backup.domain.storage_target import StorageTarget

__all__ = ["BackupPlan", "BackupStep", "build_backup_plan"]


class BackupStep(StrEnum):
    """One step of a backup. The value is the ``phase`` name its record carries."""

    DUMP = "DUMP"
    COLLECT_SITE_ROOT = "COLLECT_SITE_ROOT"
    ENCRYPT = "ENCRYPT"
    WRITE_ARCHIVE = "WRITE_ARCHIVE"
    VERIFY = "VERIFY"
    PUBLISH = "PUBLISH"


@dataclass(frozen=True, slots=True)
class BackupPlan:
    """What a backup will do, in order."""

    scope: BackupScope
    target: StorageTarget
    steps: tuple[BackupStep, ...]


def build_backup_plan(
    site: SiteDescriptor,
    scope: BackupScope,
    target: StorageTarget,
    *,
    encrypted: bool,
) -> BackupPlan:
    """The plan for backing ``site`` up to ``target``.

    For :attr:`BackupScope.ALL`: ``DUMP, COLLECT_SITE_ROOT, [ENCRYPT,]
    WRITE_ARCHIVE, VERIFY, PUBLISH``, with ``ENCRYPT`` present exactly when
    ``encrypted`` is true.
    """
    del site  # E002-S01-002's per-scope plans read it; ALL needs nothing from it.
    steps: list[BackupStep] = [BackupStep.DUMP, BackupStep.COLLECT_SITE_ROOT]
    if encrypted:
        steps.append(BackupStep.ENCRYPT)
    steps.extend((BackupStep.WRITE_ARCHIVE, BackupStep.VERIFY, BackupStep.PUBLISH))
    return BackupPlan(scope=scope, target=target, steps=tuple(steps))
