"""FR-2 — each Backup Scope end to end against the CI database.

Same environment as ``test_backup_end_to_end.py``: the CI ``db`` service via
``DB_HOST``/``DB_PASSWORD``, skipped when ``DB_HOST`` is unset. For ``files``
the Site's ``wp-config.php`` names a database host that does not resolve
(``.invalid``, RFC 6761) — a test cannot stop the job's service container, and
a host nothing answers on is the same "database down" from this package's side.
"""

from __future__ import annotations

import os
import tarfile
import tempfile
from pathlib import Path

import pytest

pytest.importorskip("l3io.wp.database")

from l3io.wp.backup.cli import main
from l3io.wp.backup.domain.archive_format import (
    MANIFEST_MEMBER,
    PAYLOAD_MEMBER,
    Manifest,
    MemberRole,
    manifest_from_json,
)
from l3io.wp.backup.domain.backup_scope import BackupScope

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        "DB_HOST" not in os.environ, reason="needs the CI database (DB_HOST unset)"
    ),
]

FIXTURE = Path(__file__).resolve().parents[1] / "data" / "wp-config.php"
DB_NAME = os.environ.get("DB_NAME", "wptest")
DB_USER = os.environ.get("DB_USER", "root")
UNREACHABLE_HOST = "db.invalid"

#: The CLI token for each scope, and the member roles its Archive holds.
CASES: dict[BackupScope, tuple[str, set[str]]] = {
    BackupScope.FILES: ("files", {MemberRole.CONFIG.value, MemberRole.SITE_ROOT.value}),
    BackupScope.DATABASE: ("db", {MemberRole.DUMP.value, MemberRole.CONFIG.value}),
    BackupScope.ALL: (
        "all",
        {MemberRole.DUMP.value, MemberRole.CONFIG.value, MemberRole.SITE_ROOT.value},
    ),
}


def _site(root: Path, *, db_host: str) -> Path:
    (root / "wp-content" / "uploads").mkdir(parents=True)
    (root / "index.php").write_text("<?php\n// index\n", encoding="utf-8")
    (root / "wp-content" / "uploads" / "a.bin").write_bytes(os.urandom(1024))
    text = FIXTURE.read_text(encoding="utf-8")
    for constant, fixture_value, value in (
        ("DB_NAME", "wordpress", DB_NAME),
        ("DB_USER", "wordpress", DB_USER),
        ("DB_PASSWORD", "pass@word1", os.environ["DB_PASSWORD"]),
        ("DB_HOST", "wordpressdb", db_host),
    ):
        old = f"define( '{constant}', '{fixture_value}' );"
        assert old in text, f"tests/data/wp-config.php no longer defines {constant}"
        text = text.replace(old, f"define( '{constant}', '{value}' );")
    (root / "wp-config.php").write_text("<?php\n" + text, encoding="utf-8")
    return root


def _read(final: Path) -> tuple[Manifest, set[str]]:
    with tarfile.open(final, mode="r:") as outer:
        assert outer.getnames() == [MANIFEST_MEMBER, PAYLOAD_MEMBER]
        manifest_file = outer.extractfile(MANIFEST_MEMBER)
        payload_file = outer.extractfile(PAYLOAD_MEMBER)
        assert manifest_file is not None
        assert payload_file is not None
        manifest = manifest_from_json(manifest_file.read().decode("utf-8"))
        with tarfile.open(fileobj=payload_file, mode="r|gz") as inner:
            roles = {info.name.split("/")[0] for info in inner}
    return manifest, roles


@pytest.mark.parametrize("scope", list(BackupScope))
def test_each_scope_publishes_exactly_its_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: BackupScope
) -> None:
    token, expected_roles = CASES[scope]
    db_host = UNREACHABLE_HOST if scope is BackupScope.FILES else os.environ["DB_HOST"]
    site = _site(tmp_path / "www", db_host=db_host)
    out = tmp_path / "out"
    out.mkdir()
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    monkeypatch.chdir(out)
    monkeypatch.setenv("TMPDIR", str(scratch))
    monkeypatch.setattr(tempfile, "tempdir", None)
    final = out / f"{token}.tar.gz"

    argv = ["backup", "--wp-dir", str(site), "--archive", str(final), "--mode", token]
    assert main(argv) == 0

    manifest, roles = _read(final)
    assert roles == expected_roles
    assert MemberRole.CONFIG.value in roles
    assert manifest.backup_scope == scope.value
    assert manifest.dump_present is (scope is not BackupScope.FILES)
    # Nothing but the published Archive in the working directory, and the
    # Run workspace is gone.
    assert list(out.iterdir()) == [final]
    assert list(scratch.iterdir()) == []
    assert not list(site.rglob("*.sql"))
