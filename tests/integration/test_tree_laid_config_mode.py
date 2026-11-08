"""BL-E008-023 — a config laid down by the tree loses "other" before the cutover.

A v0 Archive has no ``config``-role member: its ``wp-root/wp-config.php``,
stored world-readable (``0644``), is laid down by the tree extraction into a
fresh, config-less Site Root, and the relocating restore's cutover then writes
the relocated account's password into it. Through ``wpbackup2 restore`` (the
real root) over the CI database:

* the laid-down ``wp-config.php`` has no "other" permission bit — the
  by-role path's restriction now applies to the tree path too (other bits
  dropped, owner and group kept, so ``0644`` becomes ``0640``);
* one WARNING ``config_mode_restricted`` names the Archive's ``0644`` and the
  ``0640`` it became;
* the relocated user and password are in the file.

Needs the CI ``db`` service: skips without ``DB_HOST`` and fails, rather than
skips, when ``CI`` is set.
"""

from __future__ import annotations

import io
import os
import stat
import tarfile
import uuid
from pathlib import Path

import pytest

pytest.importorskip("l3io.wp.database")
pytest.importorskip("pymysql")

from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.domain.restore import RestoreMode
from tests.conftest import CredentialSentinel
from tests.integration.live_cli import (
    CliSite,
    admin_account,
    authorizing_flags,
    cli_site,
    run_cli,
)
from tests.integration.live_restore import WP_DUMP, config_text, host, read_keys, sha

__all__ = ["admin_account", "cli_site"]

pytestmark = pytest.mark.integration

ARCHIVE_MODE = 0o644
#: What the restriction leaves: "other" bits off, owner and group kept.
LAID_MODE = ARCHIVE_MODE & ~0o007


@pytest.fixture(autouse=True)
def _ci_database() -> None:
    """Skip off the Docker CI path; with ``CI`` set a missing database fails."""
    if "DB_HOST" in os.environ:
        return
    if os.environ.get("CI"):
        pytest.fail("DB_HOST is unset and CI is set: the CI database is missing")
    pytest.skip("needs the CI database (DB_HOST unset)")


def _v0_archive(work: Path, *, config: str) -> Path:
    """A v0 Archive: a WordPress-shaped Dump and a ``0644`` tree config."""
    path = work / f"v0-mode-{uuid.uuid4().hex[:6]}.tar.gz"
    members = (
        ("wp-root/index.php", b"<?php // v0\n", 0o644),
        ("wp-root/wp-config.php", config.encode(), ARCHIVE_MODE),
        ("database.sql", WP_DUMP, 0o640),
    )
    with tarfile.open(path, "w:gz") as tar:
        top = tarfile.TarInfo("wp-root")
        top.type = tarfile.DIRTYPE
        top.mode = 0o755
        tar.addfile(top)
        for name, data, mode in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = mode
            tar.addfile(info, io.BytesIO(data))
    return path


def test_a_tree_laid_config_loses_other_permissions_before_the_cutover(
    cli_site: CliSite,
    credential_sentinel: CredentialSentinel,
    capsys: pytest.CaptureFixture[str],
) -> None:
    site = cli_site
    archive = _v0_archive(
        site.site.work,
        config=config_text(
            name=site.site.database,
            user=site.site.user,
            password=site.site.password,
            db_host=host(),
        ),
    )
    target = site.site.work / "fresh"
    target.mkdir()
    new_name = f"{site.site.database}_mode"
    new_user = f"wpn_{uuid.uuid4().hex[:10]}"
    site.site.extra_users.append(new_user)
    password = credential_sentinel.relocated_password
    ran = run_cli(
        site.argv(
            archive,
            *authorizing_flags(RestoreMode.ALL),
            "--archive-database",
            site.identity(),
            "--reset-existing-db-user",
            "--new-db-name",
            new_name,
            "--new-db-user",
            new_user,
            "--new-db-password",
            password,
            root=target,
        ),
        capsys,
    )
    assert ran.code == int(ExitCode.OK), ran.err[-3000:]
    config = target / "wp-config.php"
    assert config.is_file()
    mode = stat.S_IMODE(config.stat().st_mode)
    assert mode & 0o007 == 0, f"{mode:04o}"
    assert mode == LAID_MODE, f"{mode:04o}"
    (restricted,) = ran.events("config_mode_restricted")
    assert restricted["level"] == "warning"
    assert restricted["archive_mode"] == f"{ARCHIVE_MODE:04o}"
    assert restricted["mode"] == f"{LAID_MODE:04o}"
    keys = read_keys(config)
    assert keys["DB_USER"] == new_user
    assert keys["DB_NAME"] == new_name
    assert sha(keys["DB_PASSWORD"]) == sha(password)
    credential_sentinel.add("stdout", ran.out)
    credential_sentinel.add("stderr", ran.err)
    credential_sentinel.assert_clean()
