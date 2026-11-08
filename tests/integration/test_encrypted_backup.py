"""FR-4, ADR-0024, AD-26 — an encrypted Archive, end to end (E004-S01-003).

Two tiers in one file:

- **The inspector** (``@pytest.mark.integration``, needs the CI database): a
  Site and database seeded with unique sentinel strings are backed up through
  ``drive_backup`` with the cipher from ``build_payload_cipher``. The Archive
  holds exactly ``manifest.json`` and ``payload.tar.gz.age``; no sentinel
  appears in its raw bytes; keyless verification passes; and stock ``age -d``
  (CI installs 1.3.2) recovers a gzip tar whose Dump and a Site Root file equal
  their sources, ending with the inner Manifest.
- **The refusals and the restore, through the built drivers** (no server):
  the backup refusal is ``require_passphrase``'s, before ``drive_backup`` is
  called; ``drive_restore`` runs over ``tests/restore_root_rig.py`` with the
  real cipher (right passphrase restores the tree, a wrong one is
  ``PassphraseRejectedError`` with nothing changed and no Safety Snapshot, no
  cipher is ``PassphraseRequiredError`` at plan time).
"""

from __future__ import annotations

import gzip
import io
import os
import tarfile
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("l3io.wp.database")

from l3io.wp.backup.adapters.crypto import PassphraseCipher, build_payload_cipher
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.adapters.tar import verify_archive
from l3io.wp.backup.cli import backup as backup_cli
from l3io.wp.backup.cli.passphrase import require_passphrase
from l3io.wp.backup.domain.archive_format import (
    ENCRYPTED_PAYLOAD_MEMBER,
    INNER_MANIFEST_MEMBER,
    MANIFEST_MEMBER,
    MemberRole,
    manifest_from_json,
)
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.errors import (
    PassphraseRejectedError,
    PassphraseRequiredError,
    PassphraseUnavailableError,
)
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.domain.restore import RestoreMode, SnapshotPolicy
from tests.restore_harness import INDEX, write_v1
from tests.restore_root_rig import Rig

FIXTURE = Path(__file__).resolve().parents[1] / "data" / "wp-config.php"


@pytest.fixture(scope="module")
def passphrase() -> str:
    return f"encrypted-e2e-pp-{uuid.uuid4().hex}"


# --------------------------------------------------------------------------
# The backup refusal (AD-26): no passphrase, no Archive
# --------------------------------------------------------------------------


def test_encryption_requested_with_no_passphrase_is_refused_first() -> None:
    with pytest.raises(PassphraseUnavailableError) as caught:
        require_passphrase(
            explicit=None, environ={}, prompt=None, secrets=SecretRegistry()
        )
    assert caught.value.exit_code is ExitCode.USAGE
    assert build_payload_cipher(passphrase=None, secrets=SecretRegistry()) is None


# --------------------------------------------------------------------------
# The restore, through drive_restore with the real cipher (F-22)
# --------------------------------------------------------------------------


@pytest.fixture
def sealed_rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, passphrase: str) -> Rig:
    rig = Rig(tmp_path, monkeypatch)
    work = tmp_path / "sealed"
    work.mkdir()
    rig.archive_path = write_v1(work, cipher=PassphraseCipher(passphrase))  # type: ignore[arg-type]
    return rig


def _site(rig: Rig) -> dict[str, bytes]:
    return {
        str(p.relative_to(rig.site_root)): p.read_bytes()
        for p in sorted(rig.site_root.rglob("*"))
        if p.is_file()
    }


def test_the_right_passphrase_restores_the_tree(
    sealed_rig: Rig, passphrase: str
) -> None:
    sealed_rig.real_executor()
    cipher = build_payload_cipher(passphrase=passphrase, secrets=SecretRegistry())
    sealed_rig.drive(
        cipher=cipher, mode=RestoreMode.FILES, snapshot=SnapshotPolicy.SKIP
    )
    assert (sealed_rig.site_root / "index.php").read_bytes() == INDEX


def test_a_wrong_passphrase_changes_nothing(sealed_rig: Rig, passphrase: str) -> None:
    before = _site(sealed_rig)
    sealed_rig.real_executor()  # VERIFY decrypts, before SNAPSHOT
    cipher = build_payload_cipher(
        passphrase=f"wrong-{passphrase}", secrets=SecretRegistry()
    )
    with pytest.raises(PassphraseRejectedError) as caught:
        sealed_rig.drive(cipher=cipher)
    assert caught.value.exit_code is ExitCode.VERIFICATION_FAILED
    assert passphrase not in str(caught.value)
    assert sealed_rig.backups == []  # no Safety Snapshot
    assert _site(sealed_rig) == before


def test_no_cipher_for_an_encrypted_archive_is_refused_at_plan_time(
    sealed_rig: Rig,
) -> None:
    with pytest.raises(PassphraseRequiredError):
        sealed_rig.drive(cipher=None)
    assert sealed_rig.executions == []  # refused before VERIFY could run
    assert sealed_rig.backups == []


# --------------------------------------------------------------------------
# The inspector (integration: the CI database)
# --------------------------------------------------------------------------

DB_NAME = os.environ.get("DB_NAME", "wptest")
DB_USER = os.environ.get("DB_USER", "root")


@pytest.fixture
def sentinels() -> dict[str, str]:
    token = uuid.uuid4().hex
    return {
        "file": f"file-sentinel-{token}",
        "config": f"config-sentinel-{token}",
        "row": f"row-sentinel-{token}",
    }


@pytest.fixture
def seeded(tmp_path: Path, sentinels: dict[str, str]) -> Iterator[Path]:
    root = tmp_path / "www"
    (root / "wp-content").mkdir(parents=True)
    (root / "wp-content" / "secret.txt").write_text(sentinels["file"], encoding="utf-8")
    text = FIXTURE.read_text(encoding="utf-8")
    for constant, fixture_value, value in (
        ("DB_NAME", "wordpress", DB_NAME),
        ("DB_USER", "wordpress", DB_USER),
        ("DB_PASSWORD", "pass@word1", os.environ["DB_PASSWORD"]),
        ("DB_HOST", "wordpressdb", os.environ["DB_HOST"]),
    ):
        text = text.replace(
            f"define( '{constant}', '{fixture_value}' );",
            f"define( '{constant}', '{value}' );",
        )
    text += f"\ndefine( 'SENTINEL_VALUE', '{sentinels['config']}' );\n"
    (root / "wp-config.php").write_text("<?php\n" + text, encoding="utf-8")
    pymysql = pytest.importorskip("pymysql")
    connection = pymysql.connect(
        host=os.environ["DB_HOST"],
        user=DB_USER,
        password=os.environ["DB_PASSWORD"],
        database=DB_NAME,
        autocommit=True,
    )
    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS e2e_sentinel")
        cursor.execute("CREATE TABLE e2e_sentinel (id INT PRIMARY KEY, v TEXT)")
        cursor.execute("INSERT INTO e2e_sentinel VALUES (1, %s)", (sentinels["row"],))
    yield root
    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS e2e_sentinel")
    connection.close()


def _age_d(archive_payload: bytes, passphrase: str, tmp_path: Path) -> bytes:
    """Stock ``age -d``, the passphrase typed at its prompt (``pty``)."""
    from tests.adapters.test_age_interop import _drive_age, _require_age

    age = _require_age()
    sealed, plain = tmp_path / "payload.age", tmp_path / "payload.tar.gz"
    sealed.write_bytes(archive_payload)
    code, shown = _drive_age([age, "-d", "-o", str(plain), str(sealed)], passphrase)
    assert code == 0, shown
    return plain.read_bytes()


@pytest.mark.integration
@pytest.mark.skipif(
    "DB_HOST" not in os.environ, reason="needs the CI database (DB_HOST unset)"
)
def test_an_inspector_without_the_passphrase_reads_no_content(
    tmp_path: Path, seeded: Path, sentinels: dict[str, str], passphrase: str
) -> None:
    registry = SecretRegistry()
    cipher = build_payload_cipher(passphrase=passphrase, secrets=registry)
    final = tmp_path / "out" / "site.tar.gz"
    final.parent.mkdir()
    request = backup_cli.BackupRequest(
        site_root=seeded,
        scope=BackupScope.ALL,
        target=backup_cli.storage_target_from_cli(str(final)),
        wp_config=None,
        credentials=None,
        cipher=cipher,
        extra_tools=cipher.required_tools(),
    )
    backup_cli.drive_backup(request, ctx=None, secrets=registry)

    raw = final.read_bytes()
    for value in sentinels.values():
        assert value.encode() not in raw
    assert passphrase.encode() not in raw
    with tarfile.open(final, mode="r:*") as outer:
        assert outer.getnames() == [MANIFEST_MEMBER, ENCRYPTED_PAYLOAD_MEMBER]
        manifest_file = outer.extractfile(MANIFEST_MEMBER)
        payload_file = outer.extractfile(ENCRYPTED_PAYLOAD_MEMBER)
        assert manifest_file is not None
        assert payload_file is not None
        manifest_text = manifest_file.read().decode("utf-8")
        payload = payload_file.read()
    assert payload.startswith(b"age-encryption.org/v1\n")
    manifest = manifest_from_json(manifest_text)
    assert manifest.encrypted is True
    assert verify_archive(final).passed  # keyless

    plain = gzip.decompress(_age_d(payload, passphrase, tmp_path))
    with tarfile.open(fileobj=io.BytesIO(plain), mode="r:") as inner:
        names = inner.getnames()
        assert names[-1] == INNER_MANIFEST_MEMBER
        dump_name = next(n for n in names if n.startswith(f"{MemberRole.DUMP.value}/"))
        dump = inner.extractfile(dump_name)
        site_file = inner.extractfile(
            f"{MemberRole.SITE_ROOT.value}/wp-content/secret.txt"
        )
        assert dump is not None
        assert site_file is not None
        dumped = dump.read()
        assert sentinels["row"].encode() in dumped
        indexed = {m.path: m.size for m in manifest.members}
        assert len(dumped) == indexed[dump_name]  # the member the backup indexed
        assert site_file.read() == sentinels["file"].encode()
