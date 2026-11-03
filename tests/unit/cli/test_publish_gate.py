"""AD-20 — every backup root binds ``verify_or_raise`` as the publish gate."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

pytest.importorskip("l3io.wp.database")

from l3io.wp.backup.adapters import store as store_module
from l3io.wp.backup.adapters import tar as tar_module
from l3io.wp.backup.cli import adapter_sets, main

WP_CONFIG = Path(__file__).resolve().parents[2] / "data" / "wp-config.php"


def test_the_backup_root_binds_verify_or_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bound: list[object] = []

    class Recording(store_module.LocalArchiveStore):
        def __init__(self, *, verify: object) -> None:
            bound.append(verify)
            super().__init__(verify=verify)  # type: ignore[arg-type]

    monkeypatch.setattr(adapter_sets, "LocalArchiveStore", Recording)
    site = tmp_path / "site"
    site.mkdir()
    shutil.copyfile(WP_CONFIG, site / "wp-config.php")
    archive = tmp_path / "site.tar.gz"
    argv = ["backup", "--wp-dir", str(site), "--archive", str(archive)]
    assert main([*argv, "--mode", "files"]) == 0
    assert bound == [tar_module.verify_or_raise]
    assert tar_module.verify_archive(archive).passed


def test_the_structural_check_is_gone() -> None:
    assert not hasattr(tar_module, "structural_check")
