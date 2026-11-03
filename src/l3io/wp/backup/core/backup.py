"""AD-1 — the backup plan: which steps a backup runs, computed and never performed.

Pure. :func:`build_backup_plan` receives every value it needs as an argument —
it reads no path, clock or environment it was not handed (AD-12). ``scope``
and ``encrypted`` are required with no default, so no caller selects a
scope or skips encryption by omission (AD-5).

The plan is data, and is what a Dry Run reports (AD-4): it carries the active
exclusion set (``exclusion_source``/``exclusion_patterns``, keyword-required
with no default, so a real Run and a Dry Run plan the same set) and
``required_tools``, the tools preflight checks before the first effect (AD-13):
the plan's own — :data:`DUMP_REQUIREMENT` when it dumps — followed by the
caller's ``extra_tools``, de-duplicated by name, the stricter minimum winning.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import assert_never

from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.preflight import ToolRequirement, merge_requirements
from l3io.wp.backup.domain.site import SiteDescriptor
from l3io.wp.backup.domain.storage_target import StorageTarget

__all__ = ["DUMP_REQUIREMENT", "BackupPlan", "BackupStep", "build_backup_plan"]


class BackupStep(StrEnum):
    """One step of a backup. The value is the ``phase`` name its record carries."""

    DUMP = "DUMP"
    COLLECT_SITE_ROOT = "COLLECT_SITE_ROOT"
    ENCRYPT = "ENCRYPT"
    WRITE_ARCHIVE = "WRITE_ARCHIVE"
    VERIFY = "VERIFY"
    PUBLISH = "PUBLISH"


#: What ``DUMP`` needs. Its readiness is the database's ``check_dump`` (the
#: client-tool check stays the database package's), so it names no binary
#: (ADR-0015).
DUMP_REQUIREMENT = ToolRequirement(
    name="database-dump",
    minimum_version=None,
    needed_for="the database dump (DUMP)",
)


@dataclass(frozen=True, slots=True)
class BackupPlan:
    """What a backup will do, in order, and what it needs to do it."""

    scope: BackupScope
    target: StorageTarget
    steps: tuple[BackupStep, ...]
    exclusion_source: str
    exclusion_patterns: tuple[str, ...]
    required_tools: tuple[ToolRequirement, ...]


def build_backup_plan(
    site: SiteDescriptor,
    scope: BackupScope,
    target: StorageTarget,
    *,
    encrypted: bool,
    exclusion_source: str,
    exclusion_patterns: tuple[str, ...],
    extra_tools: tuple[ToolRequirement, ...] = (),
) -> BackupPlan:
    """The plan for backing ``site`` up to ``target``.

    The capture steps follow ``scope`` (FR-2): ``FILES`` collects the Site
    Root, ``DATABASE`` dumps, ``ALL`` does both, dump first. Then ``[ENCRYPT,]
    WRITE_ARCHIVE, VERIFY, PUBLISH``, with ``ENCRYPT`` present exactly when
    ``encrypted`` is true. Every scope writes, verifies and publishes a full
    Archive. ``required_tools`` is derived from the steps, then
    ``extra_tools`` (a requirement the plan cannot derive from its own inputs,
    such as the passphrase backend).
    """
    del site  # No scope's plan depends on the Site yet; the executor reads it.
    steps: list[BackupStep]
    match scope:
        case BackupScope.FILES:
            steps = [BackupStep.COLLECT_SITE_ROOT]
        case BackupScope.DATABASE:
            steps = [BackupStep.DUMP]
        case BackupScope.ALL:
            steps = [BackupStep.DUMP, BackupStep.COLLECT_SITE_ROOT]
        case _:
            assert_never(scope)
    if encrypted:
        steps.append(BackupStep.ENCRYPT)
    steps.extend((BackupStep.WRITE_ARCHIVE, BackupStep.VERIFY, BackupStep.PUBLISH))
    own = (DUMP_REQUIREMENT,) if BackupStep.DUMP in steps else ()
    return BackupPlan(
        scope=scope,
        target=target,
        steps=tuple(steps),
        exclusion_source=exclusion_source,
        exclusion_patterns=tuple(exclusion_patterns),
        required_tools=merge_requirements(own, extra_tools),
    )
