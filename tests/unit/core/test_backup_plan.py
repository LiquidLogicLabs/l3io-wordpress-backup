"""AD-1 — the backup plan's step order."""

from __future__ import annotations

import inspect
from pathlib import PurePosixPath

import pytest

from l3io.wp.backup.core.backup import BackupPlan, BackupStep, build_backup_plan
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.site import SiteDescriptor
from l3io.wp.backup.domain.storage_target import StorageTarget

SITE = SiteDescriptor(
    site_root=PurePosixPath("/srv/site"),
    db_host="localhost",
    db_port=3306,
    db_name="wptest",
    table_prefix="wp_",
    site_url=None,
    home_url=None,
    credential_source="wp-config.php",
)
TARGET = StorageTarget(scheme="file", path=PurePosixPath("/backups/site.tar.gz"))


def test_all_runs_every_step_in_order_without_encryption() -> None:
    plan = build_backup_plan(SITE, BackupScope.ALL, TARGET, encrypted=False)
    assert plan == BackupPlan(
        scope=BackupScope.ALL,
        target=TARGET,
        steps=(
            BackupStep.DUMP,
            BackupStep.COLLECT_SITE_ROOT,
            BackupStep.WRITE_ARCHIVE,
            BackupStep.VERIFY,
            BackupStep.PUBLISH,
        ),
    )


def test_encrypt_is_present_iff_encrypted_and_before_the_write() -> None:
    plan = build_backup_plan(SITE, BackupScope.ALL, TARGET, encrypted=True)
    assert plan.steps == (
        BackupStep.DUMP,
        BackupStep.COLLECT_SITE_ROOT,
        BackupStep.ENCRYPT,
        BackupStep.WRITE_ARCHIVE,
        BackupStep.VERIFY,
        BackupStep.PUBLISH,
    )
    assert plan.steps.count(BackupStep.ENCRYPT) == 1


@pytest.mark.parametrize("name", ["scope", "encrypted"])
def test_scope_and_encryption_have_no_default(name: str) -> None:
    """Neither can be selected by omission."""
    parameter = inspect.signature(build_backup_plan).parameters[name]
    assert parameter.default is inspect.Parameter.empty


def test_the_plan_is_frozen() -> None:
    plan = build_backup_plan(SITE, BackupScope.ALL, TARGET, encrypted=False)
    with pytest.raises(AttributeError):
        plan.steps = ()  # type: ignore[misc]


def test_every_step_value_is_its_phase_name() -> None:
    assert all(step.value == step.name for step in BackupStep)
