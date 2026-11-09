"""ADR-0023, AD-18.2 — an in-root ``wp-config.php`` across every Restore Mode.

E003-S02-005, driving ``execute_restore`` with ``load_live`` as its
``load_database`` slot over the real adapters (``live_restore.py``):

* a target with a ``0640`` ``wp-config.php`` of distinct content, restored
  under ``KEEP`` in each :class:`RestoreMode` (scope from the enum), keeps the
  file byte-identical and its mode: a database Mode loads the live database
  and writes no key (owner decision 49);
* a target without one gets the Archive's copy, unchanged, from a Mode that
  restores files;
* a database-only Mode onto a target with none and no lay-down loads the
  database the Archive's config names and writes no ``wp-config.php``:
  without a cutover there is nothing to refuse (``NoConfigForCutoverError``
  is a relocation's only).

Needs the CI ``db`` service (``DB_HOST``/``DB_PASSWORD``); skips without it.
"""

from __future__ import annotations

import os
import stat
from pathlib import PurePosixPath

import pytest

pytest.importorskip("l3io.wp.database")

from l3io.wp.backup.core.restore import NO_RELOCATION
from l3io.wp.backup.domain.restore import (
    RestoreMode,
    restores_database,
    restores_files,
)
from tests.conftest import CredentialSentinel
from tests.integration.live_restore import (
    LiveSite,
    build_archive,
    config_text,
    host,
    journal_for,
    live_site,
    read_keys,
    restore,
    spec_for,
)

__all__ = ["live_site"]

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        "DB_HOST" not in os.environ, reason="needs the CI database (DB_HOST unset)"
    ),
]


@pytest.mark.parametrize("mode", list(RestoreMode))
def test_a_kept_config_is_left_byte_identical(
    live_site: LiveSite, credential_sentinel: CredentialSentinel, mode: RestoreMode
) -> None:
    site = live_site
    site.config.chmod(0o640)
    before = site.config.read_text(encoding="utf-8")
    archive_config = config_text(
        name="source_db",
        user="source_user",
        password=credential_sentinel.relocated_password,
        db_host="src",
    )
    archive = build_archive(site.work, config=archive_config)
    spec = spec_for(site, archive, mode, relocation=NO_RELOCATION)
    restore(spec)
    credential_sentinel.assert_clean()

    after = site.config.read_text(encoding="utf-8")
    assert stat.S_IMODE(site.config.stat().st_mode) == 0o640
    assert after == before
    assert read_keys(site.config)["DB_NAME"] == site.database
    if restores_database(mode):
        assert "wp_restored" in site.checksums()


@pytest.mark.parametrize("mode", [m for m in RestoreMode if restores_files(m)])
def test_a_target_without_a_config_gets_the_archives_copy(
    live_site: LiveSite, mode: RestoreMode
) -> None:
    site = live_site
    archive_config = site.config.read_text(encoding="utf-8")
    site.config.unlink()
    (site.site_root / "wp-includes").mkdir()  # a WordPress root a clean accepts
    archive = build_archive(site.work, config=archive_config)
    spec = spec_for(site, archive, mode, relocation=NO_RELOCATION)
    restore(spec)

    assert site.config.is_file()
    after = site.config.read_text(encoding="utf-8")
    assert after == archive_config
    if restores_database(mode):
        assert "wp_restored" in site.checksums()


@pytest.mark.parametrize(
    "mode", [m for m in RestoreMode if restores_database(m) and not restores_files(m)]
)
def test_a_database_mode_with_no_config_loads_and_lays_nothing_down(
    live_site: LiveSite, mode: RestoreMode
) -> None:
    site = live_site
    archive_config = config_text(
        name=site.database, user=site.user, password=site.password, db_host=host()
    )
    site.config.unlink()
    archive = build_archive(site.work, config=archive_config)
    spec = spec_for(site, archive, mode, relocation=NO_RELOCATION)
    report = restore(spec)
    assert report.database is not None
    assert report.database.restored is True
    assert not site.config.exists()
    assert set(site.checksums()) == {"wp_options", "wp_restored"}
    assert site.databases() == [site.database]
    assert journal_for(site.site_root).incomplete(PurePosixPath(site.site_root)) is None
