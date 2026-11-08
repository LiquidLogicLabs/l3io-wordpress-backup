"""Regenerate the committed v1 fixture Archives (ADR-0017, ADR-0024).

``minimal.tar.gz`` is plain. ``encrypted-passphrase.tar.gz`` (E004-S01-003) is
the same Site and Dump with the payload encrypted by ``PassphraseCipher`` under
the test-only passphrase in ``encrypted-passphrase.passphrase.txt``, and so
ends with the inner Manifest (ADR-0024 amendment). ``age`` draws a random file
key and salt, so a regenerated encrypted fixture differs in bytes; its
reproducibility is asserted on the Manifest's structure, not on bytes.

Run from the project root: ``uv run python tests/fixtures/archives/v1/generate.py``.

The committed fixtures predate owner decision 21 (2026-11-07): their outer tar
is plain, not gzip-wrapped. They are kept as they are, as the backward-
compatibility fixtures every reader must still accept
(``tests/adapters/test_outer_gzip.py``); regenerating them now writes the
gzip-wrapped encoding, so do so only when a test requires it. They were
regenerated once since (2026-11-07, BL-E004-064: ``tests/data/wp-config.php``'s
DB_PASSWORD became a unique sentinel) with the outer gzip stream removed again
after writing, so they keep the plain outer tar.

Written by the real writer (``adapters/tar.write_archive``, E002-S01-010) from
``tests/data/wp-config.php``, a few-kilobyte Site Root holding an empty
directory, a relative symlink inside the Site Root and a hard-linked pair, and
a tiny Dump. ``tests/contracts/test_supported_formats.py`` reads its Manifest's
``format_version``; ``tests/integration/test_verify_cli.py`` verifies it.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from l3io.wp.backup.adapters.crypto import PassphraseCipher
from l3io.wp.backup.adapters.tar import collect_site_root, file_member, write_archive
from l3io.wp.backup.domain.archive_format import (
    CONFIG_RELATIVE,
    DUMP_RELATIVE,
    FORMAT_VERSION,
    PAYLOAD_MEMBER,
    ConfigLocation,
    Manifest,
    ManifestExclusions,
    ManifestMember,
    ManifestPayload,
    MemberRole,
)
from l3io.wp.backup.domain.exclusion import INCLUDE_EVERYTHING

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[3]
WP_CONFIG = PROJECT_ROOT / "tests" / "data" / "wp-config.php"
OUT = HERE / "minimal.tar.gz"
ENCRYPTED_OUT = HERE / "encrypted-passphrase.tar.gz"
PASSPHRASE_FILE = HERE / "encrypted-passphrase.passphrase.txt"


def read_passphrase(path: Path = PASSPHRASE_FILE) -> str:
    """The sidecar's first line that is neither empty nor a ``#`` comment."""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            return line.strip()
    msg = f"{path} holds no passphrase line"
    raise ValueError(msg)


def _site(root: Path) -> None:
    (root / "wp-content" / "uploads").mkdir(parents=True)
    (root / "wp-content" / "empty").mkdir()
    shutil.copyfile(WP_CONFIG, root / "wp-config.php")
    (root / "index.php").write_text("<?php\n// index\n", encoding="utf-8")
    (root / "wp-content" / "uploads" / "a.txt").write_text("linked\n", "utf-8")
    os.link(root / "wp-content" / "uploads" / "a.txt", root / "wp-content" / "b.txt")
    (root / "latest.php").symlink_to("index.php")


def _manifest(index: tuple[ManifestMember, ...]) -> Manifest:
    return Manifest(
        format_version=FORMAT_VERSION,
        created_at=datetime.now(UTC).isoformat(),
        backup_scope="all",
        source={"site_root": "/var/www/fixture", "db_name": "wptest"},
        encrypted=False,
        payload=ManifestPayload(name=PAYLOAD_MEMBER, size=0, sha256="0" * 64),
        members=index,
        dump_present=True,
        exclusions=ManifestExclusions(source="none", patterns=(), excluded=()),
        config_path=ConfigLocation.SITE_ROOT.value,
    )


def write(path: Path, *, passphrase: str | None) -> None:
    """Write one fixture Archive to ``path``; encrypted when ``passphrase`` is set."""
    cipher = None if passphrase is None else PassphraseCipher(passphrase)
    with tempfile.TemporaryDirectory() as scratch:
        work = Path(scratch)
        root = work / "site"
        _site(root)
        dump = work / DUMP_RELATIVE
        dump.write_text("CREATE TABLE wp_options (id INT);\n", encoding="utf-8")
        workspace = work / "workspace"
        workspace.mkdir(mode=0o700)
        members = [
            file_member(MemberRole.DUMP, DUMP_RELATIVE, dump),
            file_member(MemberRole.CONFIG, CONFIG_RELATIVE, root / CONFIG_RELATIVE),
            *collect_site_root(root, INCLUDE_EVERYTHING).members,
        ]
        with path.open("wb") as out:
            write_archive(out, _manifest, members, workspace=workspace, cipher=cipher)


def main() -> None:
    write(OUT, passphrase=None)
    write(ENCRYPTED_OUT, passphrase=read_passphrase())


if __name__ == "__main__":
    main()
