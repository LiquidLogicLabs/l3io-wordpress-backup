"""AD-13 (E008-S01-001) -- ``RootPreflight``: the platform, then routing.

``RootPreflight`` refuses an unsupported platform before anything else, sends
the plan's ``database-dump`` requirement (``core.backup.DUMP_REQUIREMENT``) to
``DumpReadinessPreflight`` and every other requirement to ``BinaryPreflight``,
and checks exactly what it is handed. ``drive_backup`` and ``drive_restore``
bind it through the one factory, in a real Run and a Dry Run alike.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

import pytest
from structlog.testing import capture_logs

from l3io.wp.backup.adapters.preflight import BinaryPreflight
from l3io.wp.backup.cli import backup as backup_cli
from l3io.wp.backup.cli import preflight as preflight_mod
from l3io.wp.backup.cli.exit import exit_code_for
from l3io.wp.backup.cli.preflight import (
    PLATFORM_NEEDED_FOR,
    DumpReadinessPreflight,
    RootPreflight,
    require_tools,
)
from l3io.wp.backup.core.backup import DUMP_REQUIREMENT, build_backup_plan
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.errors import PreflightFailedError
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.domain.preflight import ToolRequirement, ToolStatus
from l3io.wp.backup.domain.site import SiteDescriptor
from l3io.wp.backup.domain.storage_target import StorageTarget

AGE = ToolRequirement("age", (1, 3), "the encrypted payload")
BATCHPASS = ToolRequirement("age-plugin-batchpass", None, "the encrypted payload")


class Spy:
    """A ``Preflight`` recording what it was asked; every tool present."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, ...]] = []

    def check(self, requirements: Sequence[ToolRequirement]) -> Sequence[ToolStatus]:
        self.asked.append(tuple(r.name for r in requirements))
        return [
            ToolStatus(name=r.name, present=True, version=(9, 9), path="/x")
            for r in requirements
        ]


def _root(platform: str = "linux") -> tuple[RootPreflight, Spy, Spy]:
    dump, binary = Spy(), Spy()
    return (
        RootPreflight(dump=dump, binary=binary, platform=lambda: platform),
        dump,
        binary,
    )


def _database_plan_requirements(tmp_path: Path) -> tuple[ToolRequirement, ...]:
    site = SiteDescriptor(
        site_root=PurePosixPath(tmp_path),
        db_host="localhost",
        db_port=3306,
        db_name="wp",
        table_prefix="wp_",
        site_url=None,
        home_url=None,
        credential_source="wp-config",
    )
    plan = build_backup_plan(
        site,
        BackupScope.DATABASE,
        StorageTarget(scheme="file", path=PurePosixPath(tmp_path / "a.tar.gz")),
        encrypted=True,
        exclusion_source="none",
        exclusion_patterns=(),
        extra_tools=(AGE, BATCHPASS),
    )
    return tuple(plan.required_tools)


def test_the_dump_requirement_reaches_only_the_dump_preflight(tmp_path: Path) -> None:
    requirements = _database_plan_requirements(tmp_path)
    assert DUMP_REQUIREMENT in requirements
    root, dump, binary = _root()
    statuses = root.check(requirements)
    assert dump.asked == [(DUMP_REQUIREMENT.name,)]
    assert binary.asked == [("age", "age-plugin-batchpass")]
    assert [s.name for s in statuses] == [r.name for r in requirements]


def test_it_checks_exactly_what_it_is_handed() -> None:
    root, dump, binary = _root()
    assert list(root.check([])) == []
    assert dump.asked == binary.asked == []
    root.check([BATCHPASS])
    assert binary.asked == [("age-plugin-batchpass",)]
    assert dump.asked == []


@pytest.mark.parametrize("platform", ["win32", "cygwin"])
def test_an_unsupported_platform_is_refused_first(platform: str) -> None:
    root, dump, binary = _root(platform)
    with pytest.raises(PreflightFailedError) as caught:
        root.check([DUMP_REQUIREMENT, AGE])
    assert caught.value.tool == "platform"
    assert caught.value.needed_for == PLATFORM_NEEDED_FOR
    assert platform in caught.value.found
    assert exit_code_for(caught.value) is ExitCode.NOT_READY
    assert dump.asked == binary.asked == []


def test_require_tools_reaches_the_platform_check_with_no_requirements() -> None:
    """M8: an empty requirement list no longer skips ``check``."""
    root, _, _ = _root("win32")
    with pytest.raises(PreflightFailedError):
        require_tools(root, ())


@pytest.mark.usefixtures("fake_database")
@pytest.mark.parametrize("dry_run", [False, True])
def test_drive_backup_binds_the_factorys_root_preflight(
    site: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dry_run: bool
) -> None:
    built: list[RootPreflight] = []
    real = preflight_mod.root_preflight

    def recording(run: object, descriptor: object, **kwargs: str) -> RootPreflight:
        result = real(run, descriptor, **kwargs)  # type: ignore[arg-type]
        built.append(result)
        return result

    monkeypatch.setattr(backup_cli, "root_preflight", recording)
    request = backup_cli.BackupRequest(
        site_root=site,
        scope=BackupScope.FILES,
        target=backup_cli.storage_target_from_cli(str(tmp_path / "a.tar.gz")),
        wp_config=None,
        credentials=None,
        cipher=None,
        dry_run=dry_run,
    )
    with capture_logs():
        backup_cli.drive_backup(request, ctx=None)
    assert len(built) == 1
    assert isinstance(built[0]._binary, BinaryPreflight)  # noqa: SLF001
    assert isinstance(built[0]._dump, DumpReadinessPreflight)  # noqa: SLF001


@pytest.mark.usefixtures("fake_database")
def test_a_files_only_backup_on_windows_is_refused_with_nothing_written(
    site: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    target = tmp_path / "out"
    target.mkdir()
    request = backup_cli.BackupRequest(
        site_root=site,
        scope=BackupScope.FILES,
        target=backup_cli.storage_target_from_cli(str(target / "a.tar.gz")),
        wp_config=None,
        credentials=None,
        cipher=None,
    )
    with capture_logs(), pytest.raises(PreflightFailedError) as caught:
        backup_cli.drive_backup(request, ctx=None)
    assert caught.value.tool == "platform"
    assert exit_code_for(caught.value) is ExitCode.NOT_READY
    assert list(target.iterdir()) == []


def test_a_wp_cli_config_planted_beside_the_workspace_cannot_deny_the_probe(
    tmp_path: Path,
) -> None:
    """BL-E008-002: the Run workspace sits under a shared $TMPDIR, so the probe
    must not start there. The control shows the planted file would refuse one that did.
    """
    from types import SimpleNamespace

    from l3io.wp.backup.domain.errors import WpCliConfigFoundError

    workspace = tmp_path / "l3io-wp-backup-x"
    workspace.mkdir()
    (tmp_path / "wp-cli.yml").write_text("require: [x.php]\n")  # an ancestor
    requirement = ToolRequirement("wp", (2, 12), "relocation")

    def probe(*_: object) -> str:
        return "WP-CLI 2.12.0"

    control = BinaryPreflight(
        cwd=workspace, which=lambda name: f"/usr/bin/{name}", probe=probe
    )
    with pytest.raises(WpCliConfigFoundError):
        control.check([requirement])

    built = preflight_mod.binary_preflight(SimpleNamespace(workspace=workspace))  # type: ignore[arg-type]
    built._which = lambda name: f"/usr/bin/{name}"  # noqa: SLF001
    built._probe = probe  # noqa: SLF001
    (status,) = built.check([requirement])
    assert status.present
