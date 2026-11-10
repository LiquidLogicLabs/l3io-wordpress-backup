"""FR-7 — a non-empty target refuses through the real query adapters.

The ``TargetState`` is built from the real ``LocalSiteFiles`` and the real
``WpDatabase.survey_target`` over a non-empty Site Root and a populated
database; each refusing row of AD-25's table is passed to
``build_restore_plan``, which raises ``NonEmptyTargetError`` naming the part
and the authorizing flags, so ``execute_restore`` is never reached. The Site
Root tree and ``CHECKSUM TABLE`` are unchanged afterwards.

Since owner decision 57 only a database with content but no table carrying
the site's prefix needs ``--force``: one with a prefixed table is the site's
own, which the Safety Snapshot and the automatic rollback cover. So the Run's
database is one of its own, holding a single table without the ``wp_``
prefix, never CI's shared ``wptest``, where another test's ``wp_`` table
would make the database part pass for the site's. The CLI's exit code
and stderr text are E003-S02-006's ``test_restore_cli.py``.

**Credentials.** The Run authenticates — as the WordPress account and as the
administrator — only with ``live_account``'s throwaway account, whose password
is ``credential_sentinel``'s unique sentinel (``tests/conftest.py``). CI's own
password (``root``) cannot be the leak check: it is a common word, and as root
pytest's own paths (``pytest-of-root``) hold it. The CI connection only seeds
and checksums the table. ``CredentialSentinel.assert_clean`` then checks the
refusal's ``str``/``repr``/traceback, stdout, stderr and every captured log.

Needs the CI ``db`` service through ``DB_HOST`` and ``DB_PASSWORD``; skips
when ``DB_HOST`` is unset.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

import pytest

pytest.importorskip("l3io.wp.database")
pytest.importorskip("pymysql")

import pymysql
from pymysql.connections import Connection

from l3io.wp.backup.adapters.database import (
    WpDatabase,
    admin_source,
    site_descriptor_from_wp_config,
)
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.adapters.site_files import LocalSiteFiles
from l3io.wp.backup.core.restore import (
    NO_RELOCATION,
    NonEmptyTargetError,
    TargetState,
    build_restore_plan,
)
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.credentials import ExplicitCredentialSpec
from l3io.wp.backup.domain.flag_map import (
    FLAG_MAP,
    FlagCombination,
    FlagRejection,
    authorizing_combination,
    resolve_flags,
)
from l3io.wp.backup.domain.restore import (
    ConfigPolicy,
    SnapshotPolicy,
)
from l3io.wp.backup.domain.site import SiteDescriptor
from l3io.wp.backup.domain.storage_target import StorageTarget
from tests.conftest import CredentialSentinel, write_wp_config
from tests.restore_builders import view

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        "DB_HOST" not in os.environ, reason="needs the CI database (DB_HOST unset)"
    ),
]

#: Not the site's ``wp_`` prefix: another application's table (decision 57).
TABLE = "otherapp_refusal_options"

#: Every row the table maps to a Mode without ``--force``: each must refuse.
REFUSING: list[FlagCombination] = [
    flags
    for flags, entry in FLAG_MAP.items()
    if not isinstance(entry, FlagRejection) and not dict(flags)["force"]
]


def _connect(database: str | None = None) -> Connection:
    """CI's own connection: seeding and checksums only, never the Run's."""
    return pymysql.connect(
        host=os.environ["DB_HOST"],
        user=os.environ.get("DB_USER", "root"),
        password=os.environ["DB_PASSWORD"],
        database=database,
        autocommit=True,
    )


@pytest.fixture
def database(
    credential_sentinel: CredentialSentinel, live_account: str
) -> Iterator[Connection]:
    """A database of this test's own, with one unprefixed table, as the site's.

    The sentinel's ``wp-config.php`` (already the throwaway account's) is
    rewritten to name it.
    """
    name = f"wpb_refusal_{uuid.uuid4().hex[:12]}"
    admin = _connect()
    with admin.cursor() as cursor:
        cursor.execute(f"CREATE DATABASE `{name}`")
    admin.close()
    write_wp_config(
        credential_sentinel.config,
        password=credential_sentinel.password,
        user=live_account,
        database=name,
    )
    connection = _connect(name)
    with connection.cursor() as cursor:
        cursor.execute(f"CREATE TABLE {TABLE} (id INT PRIMARY KEY, v TEXT)")
        cursor.executemany(
            f"INSERT INTO {TABLE} VALUES (%s, %s)",  # noqa: S608 - constant name
            [(i, f"value-{i}") for i in range(20)],
        )
    try:
        yield connection
    finally:
        with connection.cursor() as cursor:
            cursor.execute(f"DROP DATABASE IF EXISTS `{name}`")
        connection.close()


def _checksums(connection: Connection) -> dict[str, object]:
    with connection.cursor() as cursor:
        cursor.execute("SHOW TABLES")
        tables = [row[0] for row in cursor.fetchall()]
        cursor.execute("CHECKSUM TABLE " + ", ".join(f"`{t}`" for t in tables))
        return dict(cursor.fetchall())


def _tree(root: Path) -> dict[str, tuple[int, bytes | None]]:
    return {
        str(path.relative_to(root)): (
            path.lstat().st_mtime_ns,
            path.read_bytes() if path.is_file() else None,
        )
        for path in sorted(root.rglob("*"))
    }


def _target(s: CredentialSentinel, account: str) -> tuple[SiteDescriptor, TargetState]:
    """The target as the restore root reads it, as the sentinel account."""
    secrets = SecretRegistry()
    descriptor, wordpress = site_descriptor_from_wp_config(
        s.site, config_file=s.config, environ={}, secrets=secrets
    )
    admin = admin_source(
        ExplicitCredentialSpec(account, s.password), environ={}, secrets=secrets
    )
    query = WpDatabase(wordpress, secrets=secrets, admin=admin)
    files = LocalSiteFiles()
    location = descriptor.database_location()
    root = Path(descriptor.site_root)
    target = TargetState(
        site_root_empty=files.is_empty(root),
        database=query.survey_target(
            location, table_prefix=descriptor.table_prefix, wordpress_user=None
        ),
        config_exists=files.file_exists(s.config),
        site_root=descriptor.site_root,
        site_root_entries=files.entry_count(root),
        database_location=location,
        load_target=None,
        load_location=None,
    )
    return descriptor, target


@pytest.mark.parametrize("flags", REFUSING, ids=str)
def test_a_refusing_row_changes_nothing_and_names_the_flag(
    database: Connection,
    credential_sentinel: CredentialSentinel,
    live_account: str,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    flags: FlagCombination,
) -> None:
    s = credential_sentinel
    (s.site / "wp-content").mkdir()
    files_before = _tree(s.site)
    tables_before = _checksums(database)
    descriptor, target = _target(s, live_account)
    assert not target.site_root_empty
    assert target.database is not None
    assert target.database.table_count > 0
    assert target.database.prefixed_tables == ()

    mode, overrides = resolve_flags(flags)
    with pytest.raises(NonEmptyTargetError) as caught:
        build_restore_plan(
            descriptor,
            mode,
            overrides,
            view(),
            target,
            archive=StorageTarget(scheme="file", path=PurePosixPath(tmp_path / "a")),
            relocation=NO_RELOCATION,
            snapshot=SnapshotPolicy.TAKE,
            snapshot_dir=tmp_path / "snapshots",
            config_dir=descriptor.site_root,
            config_policy=ConfigPolicy.KEEP,
        )
    err = caught.value
    scope = dict(flags)["mode"]
    assert isinstance(scope, BackupScope)
    expected = {
        BackupScope.FILES: ("files",),
        BackupScope.DATABASE: ("database",),
        BackupScope.ALL: ("files", "database"),
    }[scope]
    assert err.parts == expected
    assert err.authorizing == authorizing_combination(mode, err.parts[0])
    assert dict(err.authorizing)["force"] is True

    captured = capsys.readouterr()
    s.add_exception("refusal", err)
    s.add("refusal found", err.found)
    s.add("refusal detail", err.detail)
    s.add("stdout", captured.out)
    s.add("stderr", captured.err)
    s.assert_clean()
    assert _tree(s.site) == files_before
    assert _checksums(database) == tables_before
