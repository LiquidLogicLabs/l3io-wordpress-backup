"""Owner decision 71 — a restore started as root resolves the administrator first.

The administrator's chain (here the ``L3IO_WP_BACKUP_ADMIN_*`` pair; an AWS
secret is resolved the same way) is resolved **before** the switch to the Site
Root's owner, and the Run is handed the answer as a ``ResolvedCredentialSpec``,
so nothing only root could read (an AWS profile in root's home) is read after
the switch. Without a configured administrator nothing is resolved, and a
restore not started as root resolves nothing early.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

import pytest

pytest.importorskip("l3io.wp.database")

from l3io.wp.backup.cli import main, privileges
from l3io.wp.backup.cli import restore as restore_mod
from l3io.wp.backup.domain.credentials import ResolvedCredentialSpec
from tests.restore_harness import write_v1

if TYPE_CHECKING:
    from l3io.wp.database import CredentialSource

ADMIN_PASSWORD = "admin-before-switch-7c1e"


def _run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, root: bool
) -> tuple[list[str], dict[str, Any]]:
    site = tmp_path / "site"
    site.mkdir()
    (site / "wp-config.php").write_text(
        "<?php\ndefine('DB_NAME', 'wp');\ndefine('DB_USER', 'u');\n"
        "define('DB_PASSWORD', 'pw-xxxxxxxx');\ndefine('DB_HOST', 'localhost');\n",
        encoding="utf-8",
    )
    archive = write_v1(tmp_path)
    order: list[str] = []
    seen: dict[str, Any] = {}

    def drop(site_root: Path, **_: object) -> None:
        order.append("switch")

    def drive(request: restore_mod.RestoreRequest, **_: object) -> NoReturn:
        order.append("drive")
        seen["admin"] = request.admin
        raise SystemExit(0)

    real_resolve = restore_mod.resolve_admin_now

    def resolve(source: CredentialSource) -> ResolvedCredentialSpec:
        order.append("resolve")
        return real_resolve(source)

    monkeypatch.setattr(privileges, "running_as_root", lambda: root)
    monkeypatch.setattr(privileges, "drop_to_site_owner", drop)
    monkeypatch.setattr(restore_mod, "drive_restore", drive)
    monkeypatch.setattr(restore_mod, "resolve_admin_now", resolve)
    with pytest.raises(SystemExit):
        main(["restore", "--wp-dir", str(site), "--archive", str(archive)])
    return order, seen


def test_a_configured_administrator_is_resolved_before_the_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("L3IO_WP_BACKUP_ADMIN_USER", "root")
    monkeypatch.setenv("L3IO_WP_BACKUP_ADMIN_PASSWORD", ADMIN_PASSWORD)
    order, seen = _run(tmp_path, monkeypatch, root=True)
    assert order == ["resolve", "switch", "drive"]
    admin = seen["admin"]
    assert isinstance(admin, ResolvedCredentialSpec)
    assert admin.username == "root"
    assert admin.secret.reveal() == ADMIN_PASSWORD
    assert ADMIN_PASSWORD not in repr(admin)


def test_no_administrator_configured_resolves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("L3IO_WP_BACKUP_ADMIN_USER", raising=False)
    monkeypatch.delenv("L3IO_WP_BACKUP_ADMIN_PASSWORD", raising=False)
    order, seen = _run(tmp_path, monkeypatch, root=True)
    assert order == ["switch", "drive"]
    assert seen["admin"] is None


def test_not_root_resolves_nothing_early(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("L3IO_WP_BACKUP_ADMIN_USER", "root")
    monkeypatch.setenv("L3IO_WP_BACKUP_ADMIN_PASSWORD", ADMIN_PASSWORD)
    order, seen = _run(tmp_path, monkeypatch, root=False)
    assert order == ["switch", "drive"]
    assert not isinstance(seen["admin"], ResolvedCredentialSpec)
