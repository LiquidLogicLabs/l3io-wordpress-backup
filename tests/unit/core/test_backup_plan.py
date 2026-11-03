"""AD-1 — the backup plan's step order."""

from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path, PurePosixPath

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


# --------------------------------------------------------------------------
# FR-2 — one plan per Backup Scope
# --------------------------------------------------------------------------

CAPTURE: dict[BackupScope, tuple[BackupStep, ...]] = {
    BackupScope.FILES: (BackupStep.COLLECT_SITE_ROOT,),
    BackupScope.DATABASE: (BackupStep.DUMP,),
    BackupScope.ALL: (BackupStep.DUMP, BackupStep.COLLECT_SITE_ROOT),
}
TAIL = (BackupStep.WRITE_ARCHIVE, BackupStep.VERIFY, BackupStep.PUBLISH)


@pytest.mark.parametrize("encrypted", [False, True])
@pytest.mark.parametrize("scope", list(BackupScope))
def test_each_scope_runs_exactly_its_steps(scope: BackupScope, encrypted: bool) -> None:
    plan = build_backup_plan(SITE, scope, TARGET, encrypted=encrypted)
    middle = (BackupStep.ENCRYPT,) if encrypted else ()
    assert plan.scope is scope
    assert plan.steps == CAPTURE[scope] + middle + TAIL


def test_the_mode_defaults_tool_guards_backup_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AD-5: a defaulted ``BackupScope`` parameter is reported by the CI tool.

    The planted file sits in a tree the tool has never seen, so this also
    proves its file set is walked rather than listed.
    """
    tool_path = Path(__file__).resolve().parents[3] / "tools" / "check_mode_defaults.py"
    spec = importlib.util.spec_from_file_location("check_mode_defaults", tool_path)
    assert spec is not None
    assert spec.loader is not None
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)

    planted = tmp_path / "nested" / "planted.py"
    planted.parent.mkdir()
    planted.write_text(
        "def plan(scope: BackupScope = BackupScope.ALL) -> None: ...\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(tool, "PACKAGE_ROOT", tmp_path)
    assert tool.main() == 1
    err = capsys.readouterr().err
    assert "planted.py:1" in err
    assert "BackupScope" in err
