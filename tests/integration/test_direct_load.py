"""Owner decisions 47, 49, 50, 53, 55 — the direct load over a real database.

``execute_restore`` with the real ``check_live``/``load_live`` slots over the
real adapters (``tests/integration/live_restore.py``), and ``wpbackup2
restore`` in process (``tests/integration/live_cli.py``):

* the Dump loads straight into the live database: an extra table with the
  site's prefix is dropped, a table without it is kept with its rows
  (decisions 49, 53);
* a Dump that fails part-way is rolled back from a real Safety Snapshot: the
  database's ``CHECKSUM TABLE`` equals the pre-restore state, extra tables
  included, and the journal is finished (decision 50);
* a rollback that fails too raises ``RollbackFailedError`` naming both
  failures, the snapshot and the recovery command; the Run stays recorded
  as interrupted and the next Run refuses;
* a site whose ``DB_*`` come from ``getenv_docker()`` is restored through
  the CLI into the database its environment names, ``wp-config.php``
  unchanged (decision 47);
* without an administrator, an absent database or an account lacking a right
  is refused before anything changes, naming what is missing (decision 55).

Every database and account is uniquely named and dropped by CI's own
connection. Needs the CI ``db`` service (``DB_HOST``/``DB_PASSWORD``); skips
without it.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

import pytest

pytest.importorskip("l3io.wp.database")

from l3io.wp.backup.adapters.database_load import RollbackFailedError
from l3io.wp.backup.adapters.journal import refuse_incomplete
from l3io.wp.backup.core.restore import NO_RELOCATION
from l3io.wp.backup.core.restore_journal import (
    STEP_ROLLBACK_FAILED,
    IncompleteRunError,
)
from l3io.wp.backup.domain.errors import (
    AdminCredentialsRequiredError,
    LoadFailedError,
)
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.domain.restore import OverrideSet, RestoreMode
from tests.conftest import CredentialSentinel
from tests.integration import live_restore
from tests.integration.live_cli import (
    CliSite,
    admin_account,
    cli_site,
    run_cli,
)
from tests.integration.live_restore import (
    LiveSite,
    build_archive,
    journal_for,
    live_site,
    read_keys,
    replace_spec,
    restore,
    root_connection,
    spec_for,
)

__all__ = ["admin_account", "cli_site", "live_site"]

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        "DB_HOST" not in os.environ, reason="needs the CI database (DB_HOST unset)"
    ),
]

#: A plugin's table added after the backup: the site's prefix.
PLUGIN = "wp_plugin_extra"
#: Another application's table in the same database: no prefix.
OTHER = "other_app"


def _plant(site: LiveSite) -> None:
    with root_connection(site.database).cursor() as cursor:
        cursor.execute(f"CREATE TABLE `{PLUGIN}` (`id` int PRIMARY KEY)")
        cursor.execute(f"CREATE TABLE `{OTHER}` (`id` int PRIMARY KEY)")
        cursor.execute(f"INSERT INTO `{OTHER}` VALUES (7), (8)")  # noqa: S608


def _rows(site: LiveSite, table: str) -> list[int]:
    with root_connection(site.database).cursor() as cursor:
        cursor.execute(f"SELECT id FROM `{table}` ORDER BY id")  # noqa: S608
        return [int(r[0]) for r in cursor.fetchall()]


def test_extra_prefixed_tables_go_and_unprefixed_ones_stay(
    live_site: LiveSite, credential_sentinel: CredentialSentinel
) -> None:
    site = live_site
    _plant(site)
    spec = replace_spec(
        spec_for(
            site,
            build_archive(site.work, config=None),
            RestoreMode.DATABASE,
            relocation=NO_RELOCATION,
        ),
        overrides=OverrideSet.none(),
        no_admin=True,
    )
    report = restore(spec).database
    assert report is not None
    assert report.restored is True
    assert report.administrator is False
    assert PLUGIN in report.dropped_tables
    assert OTHER not in report.dropped_tables
    assert set(site.checksums()) == {"wp_options", "wp_restored", OTHER}
    assert _rows(site, OTHER) == [7, 8]
    assert read_keys(site.config)["DB_NAME"] == site.database
    assert site.databases() == [site.database]
    credential_sentinel.assert_clean()


def _failing_dump(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Dump that creates a table, then fails: a load that fails mid-way."""
    monkeypatch.setattr(
        live_restore,
        "WP_DUMP",
        live_restore.WP_DUMP + b"THIS IS NOT SQL;\n",
    )


def test_a_mid_load_failure_rolls_back_to_the_pre_restore_state(
    live_site: LiveSite, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = live_site
    _plant(site)
    before = site.checksums()
    _failing_dump(monkeypatch)
    archive = build_archive(site.work, config=None)
    spec = replace_spec(
        spec_for(site, archive, RestoreMode.DATABASE, relocation=NO_RELOCATION),
        snapshot=True,
    )
    with pytest.raises(LoadFailedError):
        restore(spec)
    assert site.checksums() == before
    assert set(before) == {"wp_options", "wp_original", PLUGIN, OTHER}
    journal = journal_for(site.site_root)
    assert journal.incomplete(PurePosixPath(site.site_root)) is None
    refuse_incomplete(journal, PurePosixPath(site.site_root))


class _FailSecondLoad:
    """The database command, whose second ``load_dump`` (the rollback) fails."""

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self._loads = 0

    def __getattr__(self, name: str) -> object:
        attr = getattr(self._inner, name)
        if name != "load_dump":
            return attr

        def load(*args: object, **kwargs: object) -> object:
            self._loads += 1
            if self._loads == 2:
                reason = "the load exited 1"
                raise LoadFailedError(reason, database="rollback", exit_status=1)
            return attr(*args, **kwargs)

        return load


def _wrap(label: str, port: object) -> object:
    return _FailSecondLoad(port) if label == "database" else port


def test_a_failed_rollback_names_both_failures_and_stays_interrupted(
    live_site: LiveSite, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = live_site
    _failing_dump(monkeypatch)
    spec = replace_spec(
        spec_for(
            site,
            build_archive(site.work, config=None),
            RestoreMode.DATABASE,
            relocation=NO_RELOCATION,
        ),
        snapshot=True,
    )
    with pytest.raises(RollbackFailedError) as caught:
        restore(spec, wrap=_wrap)
    error = caught.value
    assert isinstance(error.cause, LoadFailedError)
    assert isinstance(error.rollback, LoadFailedError)
    assert error.snapshot is not None
    assert Path(error.snapshot).is_file()
    assert error.snapshot in str(error)
    assert f"--archive {error.snapshot} --mode db" in str(error)
    assert error.exit_code is ExitCode.FAILED
    journal = journal_for(site.site_root)
    record = journal.incomplete(PurePosixPath(site.site_root))
    assert record is not None
    assert record.entries[-1].step == STEP_ROLLBACK_FAILED
    with pytest.raises(IncompleteRunError) as refused:
        refuse_incomplete(journal, PurePosixPath(site.site_root))
    assert error.snapshot in refused.value.undo


# --------------------------------------------------------------------------
# Decision 55: the administrator only when needed
# --------------------------------------------------------------------------


def test_an_absent_database_without_an_administrator_is_refused(
    live_site: LiveSite,
) -> None:
    site = live_site
    with root_connection().cursor() as cursor:
        cursor.execute(f"DROP DATABASE `{site.database}`")
    spec = replace_spec(
        spec_for(
            site,
            build_archive(site.work, config=None),
            RestoreMode.DATABASE,
            relocation=NO_RELOCATION,
        ),
        no_admin=True,
    )
    with pytest.raises(AdminCredentialsRequiredError) as caught:
        restore(spec)
    assert caught.value.database == site.database
    assert caught.value.missing == ("database_not_visible",)
    assert "--admin-username" in str(caught.value)
    assert site.databases() == []
    assert journal_for(site.site_root).incomplete(PurePosixPath(site.site_root)) is None


@pytest.fixture
def limited_account(credential_sentinel: CredentialSentinel) -> Iterator[str]:
    """An account that can read its database but not replace its tables."""
    user = f"wpl_{uuid.uuid4().hex[:12]}"
    with root_connection().cursor() as cursor:
        cursor.execute(
            "CREATE USER %s@'%%' IDENTIFIED BY %s", (user, credential_sentinel.password)
        )
    try:
        yield user
    finally:
        with root_connection().cursor() as cursor:
            cursor.execute("DROP USER IF EXISTS %s@'%%'", (user,))


def test_an_account_lacking_a_right_without_an_administrator_is_refused(
    live_site: LiveSite, limited_account: str
) -> None:
    site = live_site
    with root_connection().cursor() as cursor:
        cursor.execute(
            f"GRANT SELECT ON `{site.database}`.* TO %s@'%%'", (limited_account,)
        )
    before = site.checksums()
    spec = replace_spec(
        spec_for(
            site,
            build_archive(site.work, config=None),
            RestoreMode.DATABASE,
            relocation=NO_RELOCATION,
        ),
        user=limited_account,
        no_admin=True,
    )
    with pytest.raises(AdminCredentialsRequiredError) as caught:
        restore(spec)
    assert "missing_privilege" in caught.value.missing
    assert caught.value.database == site.database
    assert site.checksums() == before


# --------------------------------------------------------------------------
# Decision 47: the official image's getenv_docker() site, through the CLI
# --------------------------------------------------------------------------

DOCKER_CONFIG = (
    "<?php\n"
    "if (!function_exists('getenv_docker')) {\n"
    "\tfunction getenv_docker($env, $default) {\n"
    "\t\tif (($val = getenv($env)) !== false) { return $val; }\n"
    "\t\treturn $default;\n"
    "\t}\n"
    "}\n"
    "define( 'DB_NAME', getenv_docker('WORDPRESS_DB_NAME', 'wordpress') );\n"
    "define( 'DB_USER', getenv_docker('WORDPRESS_DB_USER', 'example username') );\n"
    "define( 'DB_PASSWORD', getenv_docker('WORDPRESS_DB_PASSWORD', 'example') );\n"
    "define( 'DB_HOST', getenv_docker('WORDPRESS_DB_HOST', 'mysql') );\n"
    "$table_prefix = getenv_docker('WORDPRESS_TABLE_PREFIX', 'wp_');\n"
)


def test_an_environment_driven_site_is_restored_into_the_named_database(
    cli_site: CliSite,
    credential_sentinel: CredentialSentinel,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    site = cli_site
    archive = site.archive()
    _plant(site.site)
    site.site.config.write_text(DOCKER_CONFIG, encoding="utf-8")
    db_host = f"{live_restore.host()}:{live_restore.port()}"
    for name, value in {
        "WORDPRESS_DB_NAME": site.site.database,
        "WORDPRESS_DB_USER": site.site.user,
        "WORDPRESS_DB_PASSWORD": site.site.password,
        "WORDPRESS_DB_HOST": db_host,
    }.items():
        monkeypatch.setenv(name, value)
    ran = run_cli(site.argv(archive, "--mode", "db"), capsys)
    for name in ("WORDPRESS_DB_NAME", "WORDPRESS_DB_USER", "WORDPRESS_DB_PASSWORD"):
        monkeypatch.delenv(name)  # the site's variables hold the password by design
    monkeypatch.delenv("WORDPRESS_DB_HOST")
    assert ran.code == int(ExitCode.OK), ran.err[-3000:]
    assert site.site.config.read_text(encoding="utf-8") == DOCKER_CONFIG
    assert set(site.site.checksums()) == {"wp_options", "wp_restored", OTHER}
    assert site.site.databases() == [site.site.database]
    (dropped,) = ran.events("database_tables_dropped")
    assert PLUGIN in dropped["tables"]
    credential_sentinel.add("stdout", ran.out)
    credential_sentinel.add("stderr", ran.err)
    credential_sentinel.assert_clean()
