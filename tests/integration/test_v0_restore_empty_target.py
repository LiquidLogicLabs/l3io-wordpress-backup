"""BL-E004-036, owner decision 46 — a v0 ``all`` restore onto an empty target.

A v0 (wpbackup 0.2.21) Archive keeps its ``wp-config.php`` in its tree, at
``wp-root/wp-config.php``. Its view now names that config's location,
``ConfigLocation.SITE_ROOT``, as a v1 Archive with an in-tree config does, so
the plan's cutover check finds it: restoring the Archive in ``all`` onto a
target with no Site Root content, no ``wp-config.php`` and no database
extracts the tree's copy and cuts the database over in it. Before the fix
the check matched by location, never matched the v0 member path, and the
restore was refused ``NoConfigForCutoverError`` with nothing laid down.

Two ways in, over the CI database:

* ``execute_restore`` with no relocation (``live_restore.restore``): no
  lay-down, so the tree's copy is the only config the cutover can use — the
  case that was refused;
* ``wpbackup2 restore`` with no relocation flags, the operator's path. It
  lays the tree's config down before the plan, so it passed before the fix
  too; it pins that the two paths end the same way.

Each asserts the site's ``wp-config.php`` ends with the values the cutover
sets (``DB_NAME`` the Run's load database; the account and host the
Archive's) and that the Dump is loaded into that database.

Needs the CI ``db`` service: skips without ``DB_HOST`` and fails, rather than
skips, when ``CI`` is set.
"""

from __future__ import annotations

import io
import os
import tarfile
import uuid
from pathlib import Path

import pytest

pytest.importorskip("l3io.wp.database")
pytest.importorskip("pymysql")

from l3io.wp.backup.core.restore import NO_RELOCATION, load_names, staging_name
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
from tests.integration.live_restore import (
    WP_DUMP,
    LiveSite,
    config_text,
    host,
    live_site,
    read_keys,
    restore,
    root_connection,
    sha,
    spec_for,
)

__all__ = ["admin_account", "cli_site", "live_site"]

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _ci_database() -> None:
    """Skip off the Docker CI path; with ``CI`` set a missing database fails."""
    if "DB_HOST" in os.environ:
        return
    if os.environ.get("CI"):
        pytest.fail("DB_HOST is unset and CI is set: the CI database is missing")
    pytest.skip("needs the CI database (DB_HOST unset)")


def _v0_archive(work: Path, *, config: str) -> Path:
    """A v0 Archive: ``wp-root/`` with an index and ``wp-config.php``, the Dump."""
    path = work / f"v0-empty-{uuid.uuid4().hex[:6]}.tar.gz"
    members = (
        ("wp-root/index.php", b"<?php // v0\n"),
        ("wp-root/wp-config.php", config.encode()),
        ("database.sql", WP_DUMP),
    )
    with tarfile.open(path, "w:gz") as tar:
        top = tarfile.TarInfo("wp-root")
        top.type = tarfile.DIRTYPE
        top.mode = 0o755
        tar.addfile(top)
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o640
            tar.addfile(info, io.BytesIO(data))
    return path


def _drop_database(name: str) -> None:
    with root_connection().cursor() as cursor:
        cursor.execute(f"DROP DATABASE IF EXISTS `{name}`")


def _restored_rows(database: str) -> list[int]:
    with root_connection(database).cursor() as cursor:
        cursor.execute("SELECT id FROM wp_restored ORDER BY id")
        return [int(row[0]) for row in cursor.fetchall()]


def _assert_cut_over(config: Path, *, load: str, site: LiveSite) -> None:
    """The cutover's values are in ``config``, and the Dump is in ``load``."""
    keys = read_keys(config)
    assert keys["DB_NAME"] == load
    assert keys["DB_USER"] == site.user
    assert keys["DB_HOST"] == host()
    assert sha(keys["DB_PASSWORD"]) == sha(site.password)
    assert _restored_rows(load) == [1, 2, 3]


def test_execute_restore_cuts_over_into_the_trees_config(
    live_site: LiveSite, credential_sentinel: CredentialSentinel
) -> None:
    site = live_site
    archive = _v0_archive(
        site.work,
        config=config_text(
            name=site.database, user=site.user, password=site.password, db_host=host()
        ),
    )
    for entry in site.site_root.iterdir():
        entry.unlink()
    _drop_database(site.database)
    spec = spec_for(site, archive, RestoreMode.ALL, relocation=NO_RELOCATION)
    restore(spec)
    load, staging = load_names(site.database, spec.run_id, NO_RELOCATION)
    assert staging == load
    assert (site.site_root / "index.php").read_bytes() == b"<?php // v0\n"
    _assert_cut_over(site.config, load=load, site=site)
    credential_sentinel.assert_clean()


def test_the_cli_restores_a_v0_archive_onto_an_empty_directory(
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
    _drop_database(site.site.database)
    target = site.site.work / "fresh"
    target.mkdir()
    ran = run_cli(
        site.argv(
            archive,
            *authorizing_flags(RestoreMode.ALL),
            "--archive-database",
            site.identity(),
            # The Archive's account is the throwaway live account, which exists.
            "--reset-existing-db-user",
            root=target,
        ),
        capsys,
    )
    assert ran.code == int(ExitCode.OK), ran.err[-3000:]
    (run_id,) = {r["l3io_run_id"] for r in ran.records if "l3io_run_id" in r}
    load = staging_name(site.site.database, run_id)
    assert (target / "index.php").read_bytes() == b"<?php // v0\n"
    _assert_cut_over(target / "wp-config.php", load=load, site=site.site)
    credential_sentinel.add("stdout", ran.out)
    credential_sentinel.add("stderr", ran.err)
    credential_sentinel.assert_clean()
