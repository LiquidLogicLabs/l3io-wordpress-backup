"""AD-18.2/AD-18.4 — the cutover keeps ``wp-config.php``'s mode and owner.

E003-S02-005.

A ``0640`` ``wp-config.php`` keeps mode ``0640`` (and its owner) across a
restore through the real ``load_staged`` slot, without relocation (one write,
``DB_NAME``) and with it (four writes). The journal files left behind are
handed to ``credential_sentinel``, which with every log and the error surfaces
must hold neither the live sentinel password nor the relocated account's
(``credential_sentinel.relocated_password``, the cutover's ``DB_PASSWORD``).

Needs the CI ``db`` service (``DB_HOST``/``DB_PASSWORD``); skips without it.
"""

from __future__ import annotations

import dataclasses
import os
import stat
import uuid

import pytest

pytest.importorskip("l3io.wp.database")

from l3io.wp.backup.core.restore import (
    CUTOVER_KEYS,
    NO_RELOCATION,
    staging_name,
)
from l3io.wp.backup.domain.restore import RestoreMode
from tests.conftest import CredentialSentinel
from tests.integration.live_restore import (
    LiveSite,
    build_archive,
    host,
    journal_for,
    live_site,
    read_keys,
    restore,
    sha,
    spec_for,
    survey_named,
)

__all__ = ["live_site"]

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        "DB_HOST" not in os.environ, reason="needs the CI database (DB_HOST unset)"
    ),
]


@pytest.mark.parametrize("relocated", [False, True], ids=["db-name", "relocated"])
def test_wp_config_keeps_its_mode_and_owner_across_the_cutover(
    live_site: LiveSite, credential_sentinel: CredentialSentinel, relocated: bool
) -> None:
    site = live_site
    site.config.chmod(0o640)
    before = site.config.stat()
    archive = build_archive(site.work, config=None)
    relocation = NO_RELOCATION
    new_user = new_password = None
    if relocated:
        new_user = f"wpn_{uuid.uuid4().hex[:10]}"
        new_password = credential_sentinel.relocated_password
        site.extra_users.append(new_user)
        relocation = dataclasses.replace(
            NO_RELOCATION,
            cutover_host=host(),
            cutover_user=new_user,
            cutover_password=True,
        )
    spec = spec_for(
        site,
        archive,
        RestoreMode.DATABASE,
        relocation=relocation,
        relocated_user=new_user,
        relocated_password=new_password,
    )
    report = restore(spec)

    after = site.config.stat()
    assert stat.S_IMODE(after.st_mode) == 0o640
    assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)
    keys = read_keys(site.config)
    assert keys["DB_NAME"] == staging_name(site.database, spec.run_id)
    assert report.database is not None
    assert report.database.previous_database == "retained"
    if relocated:
        assert keys["DB_USER"] == new_user
        assert sha(keys["DB_PASSWORD"]) == sha(new_password)
    else:
        assert sha(keys["DB_PASSWORD"]) == sha(site.password)
    surveyed = survey_named(site.config, user=site.user, password=site.password)
    assert surveyed.is_wordpress is True  # type: ignore[attr-defined]
    assert set(keys) == set(CUTOVER_KEYS)

    directory = journal_for(site.site_root).directory
    files = sorted(directory.rglob("*.json"))
    assert files == [directory / "completed" / f"{spec.run_id}.json"]
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        credential_sentinel.add(f"journal {path.name}", text)
    credential_sentinel.assert_clean()
