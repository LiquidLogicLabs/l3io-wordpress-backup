"""FR-1/AD-20 — ``wpbackup2 backup`` end to end against the CI database.

Needs the CI ``db`` service (MySQL 8.0 / 8.4, MariaDB 11.4) through the job's
``DB_HOST`` and ``DB_PASSWORD`` (root account, database ``wptest``); skips when
``DB_HOST`` is unset. The Site's ``wp-config.php`` is ``tests/data/wp-config.php``
with its four connection values pointed at that service. ``pymysql`` (installed
with database's extra) seeds a table and reads ``CHECKSUM TABLE``; this package
itself never talks to MySQL except through ``l3io-wordpress-database``.
"""

from __future__ import annotations

import os
import tarfile
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("l3io.wp.database")
pymysql = pytest.importorskip("pymysql")

from pymysql.connections import Connection  # noqa: E402

from l3io.wp.backup.adapters import store as store_module  # noqa: E402
from l3io.wp.backup.cli import adapter_sets, main  # noqa: E402
from l3io.wp.backup.domain.archive_format import (  # noqa: E402
    MANIFEST_MEMBER,
    PAYLOAD_MEMBER,
    MemberRole,
    ignore_unknown,
    manifest_from_json,
)
from l3io.wp.backup.domain.errors import BackupError  # noqa: E402

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        "DB_HOST" not in os.environ, reason="needs the CI database (DB_HOST unset)"
    ),
]

FIXTURE = Path(__file__).resolve().parents[1] / "data" / "wp-config.php"
DB_NAME = os.environ.get("DB_NAME", "wptest")
DB_USER = os.environ.get("DB_USER", "root")


def _connect() -> Connection:
    return pymysql.connect(
        host=os.environ["DB_HOST"],
        user=DB_USER,
        password=os.environ["DB_PASSWORD"],
        database=DB_NAME,
        autocommit=True,
    )


@pytest.fixture
def database() -> Iterator[Connection]:
    connection = _connect()
    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS e2e_options")
        cursor.execute("CREATE TABLE e2e_options (id INT PRIMARY KEY, v TEXT)")
        cursor.executemany(
            "INSERT INTO e2e_options VALUES (%s, %s)",
            [(i, f"value-{i}") for i in range(100)],
        )
    yield connection
    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS e2e_options")
    connection.close()


def _checksums(connection: Connection) -> dict[str, object]:
    with connection.cursor() as cursor:
        cursor.execute("SHOW TABLES")
        tables = [row[0] for row in cursor.fetchall()]
        cursor.execute("CHECKSUM TABLE " + ", ".join(f"`{t}`" for t in tables))
        return dict(cursor.fetchall())


@pytest.fixture
def site(tmp_path: Path) -> Path:
    root = tmp_path / "www"
    (root / "wp-content" / "uploads").mkdir(parents=True)
    (root / "wp-content" / "empty").mkdir()
    (root / "index.php").write_text("<?php\n// index\n", encoding="utf-8")
    (root / "wp-content" / "uploads" / "a.bin").write_bytes(os.urandom(4096))
    text = FIXTURE.read_text(encoding="utf-8")
    for constant, fixture_value, value in (
        ("DB_NAME", "wordpress", DB_NAME),
        ("DB_USER", "wordpress", DB_USER),
        (
            "DB_PASSWORD",
            "sentinel-v0-fixture-db-password-d2ff9b94b265",
            os.environ["DB_PASSWORD"],
        ),
        ("DB_HOST", "wordpressdb", os.environ["DB_HOST"]),
    ):
        old = f"define( '{constant}', '{fixture_value}' );"
        assert old in text, f"tests/data/wp-config.php no longer defines {constant}"
        text = text.replace(old, f"define( '{constant}', '{value}' );")
    (root / "wp-config.php").write_text("<?php\n" + text, encoding="utf-8")
    return root


def _snapshot(root: Path) -> dict[str, tuple[int, bytes | None]]:
    return {
        str(path.relative_to(root)): (
            path.lstat().st_mtime_ns,
            path.read_bytes() if path.is_file() else None,
        )
        for path in sorted(root.rglob("*"))
    }


def test_a_backup_produces_one_archive_and_leaves_the_site_unchanged(
    tmp_path: Path, site: Path, database: Connection
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    final = out / "site.tar.gz"
    files_before = _snapshot(site)
    tables_before = _checksums(database)

    assert main(["backup", "--wp-dir", str(site), "--archive", str(final)]) == 0

    assert list(out.iterdir()) == [final]
    with tarfile.open(final, mode="r:*") as outer:
        assert outer.getnames() == [MANIFEST_MEMBER, PAYLOAD_MEMBER]
        manifest_file = outer.extractfile(MANIFEST_MEMBER)
        payload_file = outer.extractfile(PAYLOAD_MEMBER)
        assert manifest_file is not None
        assert payload_file is not None
        manifest = manifest_from_json(
            manifest_file.read().decode("utf-8"), on_unknown=ignore_unknown
        )
        with tarfile.open(fileobj=payload_file, mode="r|gz") as inner:
            names = [info.name for info in inner]
    roles = [name.split("/")[0] for name in names]
    assert roles[0] == MemberRole.DUMP.value
    assert roles[1] == MemberRole.CONFIG.value
    assert set(roles[2:]) == {MemberRole.SITE_ROOT.value}
    assert [m.path for m in manifest.members] == names
    assert manifest.dump_present is True

    assert _snapshot(site) == files_before
    assert _checksums(database) == tables_before


def test_a_failing_verifier_leaves_no_final_name(
    tmp_path: Path, site: Path, database: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    final = out / "site.tar.gz"

    class ForcedFailure(BackupError):
        pass

    def refuse(path: Path) -> None:
        msg = "forced verifier failure"
        raise ForcedFailure(msg)

    real_store = store_module.LocalArchiveStore
    # real_adapters builds the store; patch it where it is looked up.
    monkeypatch.setattr(
        adapter_sets, "LocalArchiveStore", lambda *, verify: real_store(verify=refuse)
    )
    assert main(["backup", "--wp-dir", str(site), "--archive", str(final)]) != 0
    assert list(out.iterdir()) == []
