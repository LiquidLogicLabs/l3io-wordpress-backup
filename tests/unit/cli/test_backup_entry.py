"""AD-16 — ``backup(..., ctx=)``, the entry point a Safety Snapshot calls."""

from __future__ import annotations

import json
import tarfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from structlog.contextvars import merge_contextvars
from structlog.testing import capture_logs

from l3io.wp.backup.adapters.database import (
    locate_wp_config,
    site_descriptor_from_wp_config,
)
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.cli import backup as backup_cli
from l3io.wp.backup.cli import main
from l3io.wp.backup.cli.adapter_sets import real_adapters
from l3io.wp.backup.cli.context import RunContext, open_run
from l3io.wp.backup.domain.archive_format import (
    MANIFEST_MEMBER,
    Integrity,
    ManifestOrigin,
    OriginKind,
)
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.exclusion import INCLUDE_EVERYTHING
from tests.unit.cli.conftest import FakeDatabase

pytestmark = pytest.mark.usefixtures("fake_database")


@pytest.fixture
def bound_run(workdir: Path) -> Iterator[RunContext]:
    with open_run(operation="restore") as ctx:
        ctx.bind_adapters(
            real_adapters(
                wordpress_source=object(),  # type: ignore[arg-type]
                secrets=ctx.secrets,
                tracer=ctx.tracer,
            )
        )
        yield ctx


def _snapshot(
    ctx: RunContext, site: Path, final: Path, origin: ManifestOrigin | None
) -> backup_cli.BackupResult:
    config = locate_wp_config(site, stated=None)
    descriptor, _ = site_descriptor_from_wp_config(
        site, config_file=config.path, environ={}, secrets=SecretRegistry()
    )
    return backup_cli.backup(
        descriptor,
        BackupScope.ALL,
        backup_cli.storage_target_from_cli(str(final)),
        ctx=ctx,
        config=config,
        cipher=None,
        exclusion=INCLUDE_EVERYTHING,
        origin=origin,
        compression_level=6,
    )


def _manifest(path: Path) -> dict[str, Any]:
    with tarfile.open(path) as tar:
        member = tar.extractfile(MANIFEST_MEMBER)
        assert member is not None
        loaded: dict[str, Any] = json.loads(member.read())
        return loaded


def test_backup_inside_a_run_opens_nothing_and_carries_its_run_id(
    bound_run: RunContext, site: Path, tmp_path: Path, workdir: Path
) -> None:
    final = tmp_path / "snap.tar.gz"
    workspaces = sorted(workdir.iterdir())
    with capture_logs(processors=[merge_contextvars]) as logs:
        result = _snapshot(bound_run, site, final, None)
    assert sorted(workdir.iterdir()) == workspaces
    events = [e["event"] for e in logs]
    assert "phase_completed" in events, "the backup logged nothing"
    assert "run_started" not in events
    assert "run_finished" not in events
    assert {e.get("l3io_run_id") for e in logs} == {bound_run.run_id}
    assert result.performed is True
    assert result.integrity is Integrity.VERIFIED
    assert final.is_file()


def test_a_given_origin_reaches_the_published_manifest(
    bound_run: RunContext, site: Path, tmp_path: Path
) -> None:
    origin = ManifestOrigin(
        kind=OriginKind.SAFETY_SNAPSHOT,
        restore_mode="full",
        run_id=bound_run.run_id,
    )
    final = tmp_path / "snap.tar.gz"
    _snapshot(bound_run, site, final, origin)
    recorded = _manifest(final)["origin"]
    assert recorded["kind"] == OriginKind.SAFETY_SNAPSHOT.value
    assert recorded["run_id"] == bound_run.run_id


def test_run_backup_reaches_the_same_entry_point_once(
    site: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_database: FakeDatabase,
) -> None:
    calls: list[dict[str, object]] = []
    real = backup_cli.backup

    def spy(*args: object, **kwargs: object) -> backup_cli.BackupResult:
        calls.append(kwargs)
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(backup_cli, "backup", spy)
    final = tmp_path / "out.tar.gz"
    assert main(["backup", "--wp-dir", str(site), "--archive", str(final)]) == 0
    assert len(calls) == 1
    assert calls[0]["origin"] is None
    assert len(fake_database.calls) == 1
