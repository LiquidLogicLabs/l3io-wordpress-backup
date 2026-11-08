"""AD-12 — the per-Run private workspace."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from l3io.wp.backup.adapters import workspace as workspace_mod
from l3io.wp.backup.adapters.workspace import (
    WORKSPACE_PREFIX,
    WorkspaceNotRemovedError,
    run_workspace,
)
from l3io.wp.backup.domain.errors import ArchiveWriteError
from l3io.wp.backup.domain.exit_codes import ExitCode


def test_the_workspace_is_owner_only_and_removed_on_success(tmp_path: Path) -> None:
    with run_workspace(tmp_path) as workspace:
        assert workspace.parent == tmp_path
        assert workspace.name.startswith(WORKSPACE_PREFIX)
        assert stat.S_IMODE(workspace.stat().st_mode) == 0o700
        (workspace / "nested").mkdir()
        (workspace / "nested" / "dump.sql").write_bytes(b"data")
    assert not workspace.exists()


def test_the_workspace_is_removed_on_an_exception(tmp_path: Path) -> None:
    class Boom(Exception):
        pass

    seen: list[Path] = []

    def fail_inside() -> None:
        with run_workspace(tmp_path) as workspace:
            seen.append(workspace)
            (workspace / "dump.sql").write_bytes(b"data")
            raise Boom

    with pytest.raises(Boom):
        fail_inside()
    assert not seen[0].exists()
    assert list(tmp_path.iterdir()) == []


def test_two_runs_get_two_workspaces(tmp_path: Path) -> None:
    with run_workspace(tmp_path) as first, run_workspace(tmp_path) as second:
        assert first != second


def test_the_default_parent_is_the_system_temporary_directory() -> None:
    with run_workspace(None) as workspace:
        assert workspace.is_dir()
    assert not workspace.exists()


def test_an_unusable_parent_is_an_archive_write_error(tmp_path: Path) -> None:
    with pytest.raises(ArchiveWriteError), run_workspace(tmp_path / "missing"):
        pass


def _unremovable(monkeypatch: pytest.MonkeyPatch) -> None:
    """``rmtree`` that removes nothing, as on a filesystem that refuses it."""

    def keep(path: object, **kwargs: object) -> None:
        del path, kwargs

    monkeypatch.setattr(workspace_mod.shutil, "rmtree", keep)


def test_an_unremovable_workspace_fails_an_otherwise_successful_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BL-E002-041: a plaintext Dump left behind is not exit 0."""
    _unremovable(monkeypatch)
    with (
        capture_logs() as logs,
        pytest.raises(WorkspaceNotRemovedError) as caught,
        run_workspace(tmp_path) as workspace,
    ):
        (workspace / "dump.sql").write_bytes(b"data")
    assert caught.value.path == workspace
    assert caught.value.exit_code is ExitCode.FAILED
    assert str(workspace) not in str(caught.value)
    assert WORKSPACE_PREFIX in str(caught.value)
    assert "workspace_not_removed" in [r["event"] for r in logs]


def test_an_unremovable_workspace_keeps_the_runs_own_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Boom(Exception):
        pass

    _unremovable(monkeypatch)
    with capture_logs() as logs, pytest.raises(Boom), run_workspace(tmp_path):
        raise Boom
    assert "workspace_not_removed" in [r["event"] for r in logs]
