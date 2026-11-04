"""ADR-0023, AD-18.2 — an in-root ``wp-config.php`` across every Restore Mode.

E003-S02-005, driving ``execute_restore`` with ``load_staged`` as its
``load_database`` slot over the real adapters (``live_restore.py``):

* a target with a ``0640`` ``wp-config.php`` of distinct content, restored
  under ``KEEP`` in each :class:`RestoreMode` (scope from the enum), keeps the
  file byte-identical except the cutover key a database Mode writes
  (``DB_NAME``), and keeps its mode;
* a target without one gets the Archive's copy from a Mode that restores
  files;
* a database-only Mode onto a target with none and no lay-down refuses
  ``NoConfigForCutoverError`` (``REFUSED``) at plan time, before ``SNAPSHOT``,
  with the database untouched and no journal begun.

Needs the CI ``db`` service (``DB_HOST``/``DB_PASSWORD``); skips without it.
"""

from __future__ import annotations

import os
import stat

import pytest

pytest.importorskip("l3io.wp.database")

from l3io.wp.backup.core.restore import (
    NO_RELOCATION,
    NoConfigForCutoverError,
    staging_name,
)
from l3io.wp.backup.domain.exit_codes import ExitCode
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


def _differing_lines(before: str, after: str) -> list[tuple[str, str]]:
    old, new = before.splitlines(), after.splitlines()
    assert len(old) == len(new)
    return [(a, b) for a, b in zip(old, new, strict=True) if a != b]


@pytest.mark.parametrize("mode", list(RestoreMode))
def test_a_kept_config_changes_only_by_the_cutover(
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
    if not restores_database(mode):
        assert after == before
        return
    staged = staging_name(site.database, spec.run_id)
    changed = _differing_lines(before, after)
    assert len(changed) == 1
    old, new = changed[0]
    assert "DB_NAME" in old
    assert site.database in old
    assert "DB_NAME" in new
    assert staged in new
    assert read_keys(site.config)["DB_NAME"] == staged


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
    if not restores_database(mode):
        assert after == archive_config
        return
    changed = _differing_lines(archive_config, after)
    assert len(changed) == 1
    assert staging_name(site.database, spec.run_id) in changed[0][1]


@pytest.mark.parametrize(
    "mode", [m for m in RestoreMode if restores_database(m) and not restores_files(m)]
)
def test_a_database_mode_with_no_config_to_cut_over_refuses_before_snapshot(
    live_site: LiveSite, mode: RestoreMode
) -> None:
    site = live_site
    archive_config = config_text(
        name=site.database, user=site.user, password=site.password, db_host=host()
    )
    site.config.unlink()
    archive = build_archive(site.work, config=archive_config)
    tables_before = site.checksums()
    spec = spec_for(site, archive, mode, relocation=NO_RELOCATION)
    with pytest.raises(NoConfigForCutoverError) as caught:
        restore(spec)
    assert caught.value.exit_code is ExitCode.REFUSED
    assert not site.config.exists()
    assert site.checksums() == tables_before
    assert site.databases() == [site.database]
    assert journal_for(site.site_root).incomplete(caught.value.path) is None  # type: ignore[arg-type]
