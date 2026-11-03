"""AD-4 — the plan is equal across the real and the no-op adapter sets.

For each ``BackupScope`` (parametrised from the enum), the same request is
driven through the root's planning path twice — once binding the real set
(the database faked at its outermost edge), once the Dry Run set — and the
two ``BackupPlan`` values that ran, and their rendered action lists, are
equal. A Dry Run reports exactly what the real Run executes.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from structlog.testing import capture_logs

pytest.importorskip("l3io.wp.database")

from l3io.wp.backup.cli import adapter_sets
from l3io.wp.backup.cli import backup as backup_cli
from l3io.wp.backup.domain.backup_scope import BackupScope
from tests.unit.cli.conftest import FakeDatabase


@pytest.fixture
def site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "tmp"
    work.mkdir()
    monkeypatch.setenv("TMPDIR", str(work))
    monkeypatch.setattr(tempfile, "tempdir", None)
    monkeypatch.setattr(
        adapter_sets, "WpDatabase", lambda credentials, *, secrets: FakeDatabase()
    )
    root = tmp_path / "www"
    root.mkdir()
    (root / "wp-config.php").write_text(
        "<?php\n"
        "define('DB_NAME', 'wptest');\n"
        "define('DB_USER', 'wp');\n"
        "define('DB_PASSWORD', 'not-a-real-secret');\n"
        "define('DB_HOST', 'db.invalid');\n"
        "$table_prefix = 'wp_';\n",
        encoding="utf-8",
    )
    (root / "index.php").write_text("<?php\n", encoding="utf-8")
    return root


def _drive(
    site: Path, final: Path, scope: BackupScope, *, dry_run: bool
) -> backup_cli.BackupResult:
    request = backup_cli.BackupRequest(
        site_root=site,
        scope=scope,
        target=backup_cli.storage_target_from_cli(str(final)),
        wp_config=None,
        credentials=None,
        cipher=None,
        dry_run=dry_run,
    )
    with capture_logs():
        return backup_cli.drive_backup(request, ctx=None)


@pytest.mark.parametrize("scope", list(BackupScope))
def test_the_real_and_dry_run_plans_are_equal(
    site: Path, tmp_path: Path, scope: BackupScope
) -> None:
    final = tmp_path / f"{scope.value}.tar.gz"
    rehearsed = _drive(site, final, scope, dry_run=True)
    assert not final.exists()
    real = _drive(site, final, scope, dry_run=False)
    assert final.is_file()

    assert (rehearsed.performed, real.performed) == (False, True)
    assert rehearsed.plan == real.plan
    assert rehearsed.actions == real.actions
    assert rehearsed.actions, "nothing was rendered"
    assert rehearsed.plan.scope is scope
