"""FR-6, ADR-0023 — the file Modes restore through the real site-files adapter.

``execute_restore`` drives the real ``LocalSiteFiles`` (and the real
``WpConfigWriter``) for ``files`` and ``files-clean``, with a fake
``take_snapshot`` and a ``load_database`` slot that fails the test if called.
Every-mode end-to-end, database Modes included, is E003-S02-006's
``test_restore_cli.py``. Needs no database server.
"""

from __future__ import annotations

from pathlib import Path
from typing import NoReturn

import pytest

from l3io.wp.backup.adapters.config_writer import WpConfigWriter
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.domain.restore import ConfigPolicy, RestoreMode
from tests.restore_harness import (
    ARCHIVE_DB,
    INDEX,
    TARGET_DB,
    Harness,
    wp_config,
    write_v1,
)

pytestmark = pytest.mark.integration

FILE_MODES = [RestoreMode.FILES, RestoreMode.FILES_CLEAN]


def _harness(tmp_path: Path, mode: RestoreMode, policy: ConfigPolicy) -> Harness:
    h = Harness(
        work=tmp_path,
        archive_path=write_v1(tmp_path),
        mode=mode,
        config_policy=policy,
        real_files=True,
        config_writer=WpConfigWriter(SecretRegistry()),
        slot=_never,
    )
    # The target has drifted: a changed index, and a file the Archive lacks.
    (h.site_root / "index.php").write_bytes(b"<?php // tampered\n")
    return h


def _never(*_: object) -> NoReturn:
    pytest.fail("a file Mode reached the load_database slot")


@pytest.mark.parametrize("mode", FILE_MODES)
def test_a_file_mode_restores_the_tree_and_keeps_the_target_config(
    tmp_path: Path, mode: RestoreMode
) -> None:
    h = _harness(tmp_path, mode, ConfigPolicy.KEEP)
    config_before = h.config_path.read_bytes()
    old_before = (h.site_root / "old.php").read_bytes()
    report = h.run()
    assert (h.site_root / "index.php").read_bytes() == INDEX
    # KEEP: the target's wp-config.php is excluded explicitly, byte-identical.
    assert h.config_path.read_bytes() == config_before
    assert report.wp_config_kept
    if mode is RestoreMode.FILES_CLEAN:
        assert not (h.site_root / "old.php").exists()
    else:
        assert (h.site_root / "old.php").read_bytes() == old_before
    assert h.snapshots  # the fake snapshot ran before the destructive phases


@pytest.mark.parametrize("mode", FILE_MODES)
def test_overwrite_lands_the_archives_config_with_the_targets_db_values(
    tmp_path: Path, mode: RestoreMode
) -> None:
    h = _harness(tmp_path, mode, ConfigPolicy.OVERWRITE)
    before = h.config_path.read_bytes()
    h.run()
    text = h.config_path.read_text(encoding="utf-8")
    reader = WpConfigWriter(SecretRegistry())
    landed = reader.read_constants(h.config_path, list(TARGET_DB))
    assert dict(landed) == TARGET_DB
    assert ARCHIVE_DB["DB_NAME"] not in text
    # ADV-11: the workspace copy is removed once the replace is done; the
    # Archive's config is the one write_v1 wrote.
    assert not (h.workspace / "archive-wp-config.php").exists()
    archive_config = wp_config(ARCHIVE_DB)
    non_db = [line for line in archive_config.splitlines() if "DB_" not in line]
    assert [line for line in text.splitlines() if "DB_" not in line] == non_db
    saved = Path(h.header().saved_config or "")
    assert saved.read_bytes() == before
    assert saved.stat().st_mode & 0o777 == 0o600
