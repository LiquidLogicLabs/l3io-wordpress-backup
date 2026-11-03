"""FR-5/ADR-0019 — a Dry Run reports the exclusion set it plans, never its effect.

The collection line names the policy's patterns and source; the
``planned_action`` record carries both; and the policy's ``decide`` is called
zero times, because a Dry Run reads no Site Root content.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import pytest
from structlog.testing import capture_logs

from l3io.wp.backup.cli import backup as backup_cli
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.exclusion import ExclusionDecision

pytestmark = pytest.mark.usefixtures("fake_database")


@dataclass
class SpyPolicy:
    """An ``ExclusionPolicy`` that counts every decision asked of it."""

    source: str = "custom"
    patterns: tuple[str, ...] = ("wp-content/cache/", "*.log")
    decided: list[str] = field(default_factory=list)

    def decide(self, relative: PurePosixPath, *, is_dir: bool) -> ExclusionDecision:
        self.decided.append(str(relative))
        return ExclusionDecision(excluded=False, pattern=None, protected=False)


def _rehearse(site: Path, final: Path, policy: SpyPolicy, scope: BackupScope) -> tuple:
    request = backup_cli.BackupRequest(
        site_root=site,
        scope=scope,
        target=backup_cli.storage_target_from_cli(str(final)),
        wp_config=None,
        credentials=None,
        cipher=None,
        exclusion=policy,
        dry_run=True,
    )
    with capture_logs() as logs:
        result = backup_cli.drive_backup(request, ctx=None)
    return result, logs


def test_the_collect_line_names_both_patterns_and_the_source(
    site: Path, tmp_path: Path
) -> None:
    policy = SpyPolicy()
    result, logs = _rehearse(site, tmp_path / "a.tar.gz", policy, BackupScope.ALL)
    (collect,) = [a for a in result.actions if "collect Site Root" in a]
    assert "excluding 2 patterns" in collect
    assert "custom: wp-content/cache/, *.log" in collect
    (record,) = [
        e
        for e in logs
        if e["event"] == "planned_action" and e["step"] == "COLLECT_SITE_ROOT"
    ]
    assert record["exclusion_source"] == "custom"
    assert record["exclusion_patterns"] == ["wp-content/cache/", "*.log"]
    assert policy.decided == []


def test_a_database_plan_has_no_collect_line_but_carries_the_set(
    site: Path, tmp_path: Path
) -> None:
    policy = SpyPolicy()
    result, _ = _rehearse(site, tmp_path / "db.tar.gz", policy, BackupScope.DATABASE)
    assert not [a for a in result.actions if "collect" in a]
    assert result.plan.exclusion_source == "custom"
    assert result.plan.exclusion_patterns == policy.patterns
    assert policy.decided == []


def test_a_real_run_with_the_same_policy_does_decide(
    site: Path, tmp_path: Path
) -> None:
    """The spy's teeth: the same request, really run, asks the policy."""
    policy = SpyPolicy()
    request = backup_cli.BackupRequest(
        site_root=site,
        scope=BackupScope.FILES,
        target=backup_cli.storage_target_from_cli(str(tmp_path / "real.tar.gz")),
        wp_config=None,
        credentials=None,
        cipher=None,
        exclusion=policy,
    )
    with capture_logs():
        backup_cli.drive_backup(request, ctx=None)
    assert policy.decided
