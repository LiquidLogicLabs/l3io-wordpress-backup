"""BL-E004-049 — the right passphrase restores an encrypted Archive's Dump.

``test_encrypted_backup.py``'s right-passphrase ``drive_restore`` case runs
``FILES`` only. Here an Archive encrypted by the real writer (the real
``PassphraseCipher``: the ``l3io-age-passphrase`` binder, or ``age`` 1.3.2
under ``L3IO_AGE_PASSPHRASE_BACKEND=cli``) is restored by ``api.restore`` —
which composes through ``drive_restore`` (ADR-0025) — in each Dump-loading
Mode over the CI database: the Archive's table and rows arrive in the live
database, and for ``all-clean`` the Site Root is the Archive's tree.

Uniquely named throwaway databases and accounts (``live_site``,
``admin_account``), dropped at teardown. Needs the CI database; skips when
``DB_HOST`` is unset.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

pytest.importorskip("l3io.wp.database")
pytest.importorskip("pymysql")

from l3io.wp.backup import api
from l3io.wp.backup.adapters.crypto import PassphraseCipher
from l3io.wp.backup.adapters.tar import collect_site_root, file_member, write_archive
from l3io.wp.backup.domain.archive_format import (
    DUMP_RELATIVE,
    FORMAT_VERSION,
    PAYLOAD_MEMBER,
    Manifest,
    ManifestMember,
    ManifestPayload,
    MemberRole,
)
from l3io.wp.backup.domain.credentials import ExplicitCredentialSpec
from l3io.wp.backup.domain.exclusion import INCLUDE_EVERYTHING
from l3io.wp.backup.domain.restore import (
    ConfigPolicy,
    Override,
    OverrideSet,
    RestoreMode,
    SnapshotPolicy,
    restores_files,
)
from tests.conftest import CredentialSentinel
from tests.integration.live_cli import admin_account
from tests.integration.live_restore import (
    INDEX,
    WP_DUMP,
    LiveSite,
    config_text,
    host,
    live_site,
    read_keys,
    root_connection,
)

__all__ = ["admin_account", "live_site"]

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        "DB_HOST" not in os.environ, reason="needs the CI database (DB_HOST unset)"
    ),
]


def _waivers(mode: RestoreMode) -> OverrideSet:
    """``--force`` for exactly the parts ``mode`` restores (the targets are live)."""
    waived = {Override.NONEMPTY_DATABASE}
    if restores_files(mode):
        waived.add(Override.NONEMPTY_FILES)
    return OverrideSet(waived=frozenset(waived))


def _sealed(site: LiveSite, passphrase: str) -> Path:
    """A v1 Archive of a new tree, its config naming ``site``'s database, the Dump."""
    source = site.work / f"sealed-source-{uuid.uuid4().hex[:6]}"
    source.mkdir()
    (source / "index.php").write_bytes(INDEX)
    (source / "wp-config.php").write_text(
        config_text(
            name=site.database, user=site.user, password=site.password, db_host=host()
        ),
        encoding="utf-8",
    )
    dump = site.work / f"sealed-dump-{uuid.uuid4().hex[:6]}.sql"
    dump.write_bytes(WP_DUMP)
    members = [
        file_member(MemberRole.DUMP, DUMP_RELATIVE, dump),
        *collect_site_root(source, INCLUDE_EVERYTHING).members,
    ]

    def manifest(index: tuple[ManifestMember, ...]) -> Manifest:
        return Manifest(
            format_version=FORMAT_VERSION,
            created_at="2026-11-07T00:00:00Z",
            backup_scope="all",
            source={"db_name": "source_db"},
            encrypted=False,
            payload=ManifestPayload(name=PAYLOAD_MEMBER, size=0, sha256="0" * 64),
            members=index,
            dump_present=True,
            config_path="wp-config.php",
        )

    workspace = site.work / f"sealed-write-{uuid.uuid4().hex[:6]}"
    workspace.mkdir(mode=0o700)
    path = site.work / f"sealed-{uuid.uuid4().hex[:6]}.tar.gz"
    with path.open("wb") as out:
        written = write_archive(
            out,
            manifest,
            members,
            workspace=workspace,
            cipher=PassphraseCipher(passphrase),
        )
    assert written.encrypted
    return path


def _rows(database: str) -> list[int]:
    with root_connection(database).cursor() as cursor:
        cursor.execute("SELECT id FROM `wp_restored` ORDER BY id")
        return [int(row[0]) for row in cursor.fetchall()]


@pytest.mark.parametrize(
    "mode",
    [RestoreMode.DATABASE, RestoreMode.ALL_CLEAN],
    ids=lambda m: m.value,
)
def test_the_right_passphrase_loads_the_dump(
    live_site: LiveSite,
    admin_account: str,
    credential_sentinel: CredentialSentinel,
    mode: RestoreMode,
) -> None:
    passphrase = f"sentinel-pp-{uuid.uuid4().hex}"
    archive = _sealed(live_site, passphrase)
    old = live_site.site_root / "old-file.php"
    old.write_text("<?php\n// before the restore\n", encoding="utf-8")
    outcome = api.restore(
        live_site.site_root,
        archive=archive,
        mode=mode,
        overrides=_waivers(mode),
        snapshot=SnapshotPolicy.TAKE,
        config_policy=ConfigPolicy.KEEP,
        snapshot_dir=live_site.work / "snapshots",
        admin=ExplicitCredentialSpec(admin_account, credential_sentinel.admin_password),
        passphrase=passphrase,
    )
    assert outcome.snapshot_encrypted is True  # the same passphrase sealed it
    live = str(read_keys(live_site.config)["DB_NAME"])
    assert live.startswith(live_site.database)
    assert _rows(live) == [1, 2, 3]
    if restores_files(mode):
        assert (live_site.site_root / "index.php").read_bytes() == INDEX
        assert not old.exists()
    else:
        assert old.exists()
    credential_sentinel.add_repr("outcome", outcome)
    credential_sentinel.assert_clean()
    surfaces = credential_sentinel.surfaces()
    assert not [name for name, text in surfaces if passphrase in text]
