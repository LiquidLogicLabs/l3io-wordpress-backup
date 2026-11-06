"""AD-6 — ``domain/archive_format.py`` is the one owner of the archive format.

No other module under ``src/`` may spell a member name or a role prefix: every
string constant (f-string parts included) is checked against the literals
below, the v0 (0.2.21) names included (E004-S01-002, F-10): ``database.sql``
matched as a whole constant, ``wp-root`` exactly and as the prefix ``wp-root/``.
Scope is ``src/`` walked for ``*.py`` files, asserted to contain the
owner, and the walker is run over a planted tree that hides the literal in a
nested directory and inside an f-string.

That is half of "nothing in the product writes v0". The other half drives the
real writer, ``TarArchiveWriter().write``, once per ``BackupScope`` member (from
the enum, with the inputs the scope's own plan selects) and asserts each Archive
classifies as v1 with ``manifest.json`` first.

Not covered, stated so the next reader keeps looking: a literal assembled at
runtime (``"site" + "-root/"``) and the Manifest's key set when spelled in
another module; review covers those.
"""

from __future__ import annotations

import ast
import io
import shutil
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import pytest

from l3io.wp.backup.adapters.tar import TarArchiveProbe, TarArchiveWriter
from l3io.wp.backup.core.backup import BackupStep, build_backup_plan
from l3io.wp.backup.domain.archive_format import (
    FORMAT_VERSION,
    MANIFEST_MEMBER,
    PAYLOAD_MEMBER,
    ExcludedEntry,
    Manifest,
    ManifestExclusions,
    ManifestMember,
    ManifestPayload,
    classify,
)
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.exclusion import INCLUDE_EVERYTHING
from l3io.wp.backup.domain.site import SiteDescriptor
from l3io.wp.backup.domain.storage_target import StorageTarget
from l3io.wp.backup.ports.archive import ManifestBuilder

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WP_CONFIG = PROJECT_ROOT / "tests" / "data" / "wp-config.php"
SRC = PROJECT_ROOT / "src"
OWNER = SRC / "l3io" / "wp" / "backup" / "domain" / "archive_format.py"

#: Member names, matched exactly.
NAMES = ("manifest.json", "payload.tar.gz")
#: Role prefixes, matched at the start of a constant.
PREFIXES = ("dump/", "site-root/", "config/", "wp-root/")
#: v0 member names, matched as the whole constant.
EXACT = ("database.sql", "wp-root")


def _offenders(files: list[Path], owner: Path) -> list[str]:
    found = []
    for path in files:
        if path == owner:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            value = node.value
            if (
                value.startswith(PREFIXES)
                or value in EXACT
                or any(name in value for name in NAMES)
            ):
                found.append(f"{path.name}:{node.lineno}: {value!r}")
    return found


def test_the_owner_is_in_scope() -> None:
    files = sorted(SRC.rglob("*.py"))
    assert OWNER in files, f"AD-6 scope missing the owner: {OWNER}"


def test_only_the_owner_spells_a_member_name() -> None:
    offenders = _offenders(sorted(SRC.rglob("*.py")), OWNER)
    assert not offenders, (
        f"AD-6 — import these from domain/archive_format.py: {offenders}"
    )


def test_the_grep_finds_literals_hidden_deep_and_in_f_strings(tmp_path: Path) -> None:
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    (deep / "leak.py").write_text(
        "X = 'manifest.json'\n"
        "def name(rel):\n"
        "    return f'site-root/{rel}'\n"
        "Y = 'payload.tar.gz.age'\n"
        "V = 'database.sql'\n"
        "def v0(rel):\n"
        "    return f'wp-root/{rel}'\n"
        "W = 'wp-root'\n"
        "OK = 'a database.sql dump, not a member name'\n",
        encoding="utf-8",
    )
    owner = tmp_path / "archive_format.py"
    owner.write_text("Z = 'manifest.json'\n", encoding="utf-8")
    found = _offenders(sorted(tmp_path.rglob("*.py")), owner)
    assert sorted(found) == [
        "leak.py:1: 'manifest.json'",
        "leak.py:3: 'site-root/'",
        "leak.py:4: 'payload.tar.gz.age'",
        "leak.py:5: 'database.sql'",
        "leak.py:7: 'wp-root/'",
        "leak.py:8: 'wp-root'",
    ]


def _manifest_builder(scope: BackupScope, *, dump: bool) -> ManifestBuilder:
    def build(
        index: tuple[ManifestMember, ...], excluded: tuple[ExcludedEntry, ...]
    ) -> Manifest:
        return Manifest(
            format_version=FORMAT_VERSION,
            created_at=datetime.now(UTC).isoformat(),
            backup_scope=scope.value,
            source={"site_root": "/var/www/site", "db_name": "wptest"},
            encrypted=False,
            payload=ManifestPayload(name=PAYLOAD_MEMBER, size=0, sha256="0" * 64),
            members=index,
            dump_present=dump,
            exclusions=ManifestExclusions(source="none", patterns=(), excluded=()),
        )

    return build


@pytest.mark.parametrize("scope", list(BackupScope), ids=lambda s: s.value)
def test_every_backup_scope_writes_v1(tmp_path: Path, scope: BackupScope) -> None:
    site_root = tmp_path / "site"
    site_root.mkdir()
    shutil.copyfile(WP_CONFIG, site_root / "wp-config.php")
    (site_root / "index.php").write_bytes(b"<?php\n")
    dump = tmp_path / "dump.sql"
    dump.write_bytes(b"CREATE TABLE t (id INT);\n")
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    archive = tmp_path / "out.tar.gz"
    site = SiteDescriptor(
        site_root=PurePosixPath(site_root), db_host="db.invalid", db_port=3306,
        db_name="wptest", table_prefix="wp_", site_url=None, home_url=None,
        credential_source="wp-config.php",
    )  # fmt: skip
    target = StorageTarget(scheme="file", path=PurePosixPath(archive))
    plan = build_backup_plan(
        site, scope, target, encrypted=False,
        exclusion_source="none", exclusion_patterns=(),
    )  # fmt: skip
    dumps = BackupStep.DUMP in plan.steps
    collects = BackupStep.COLLECT_SITE_ROOT in plan.steps
    out = io.BytesIO()
    TarArchiveWriter().write(
        out,
        site_root=site_root if collects else None,
        dump=dump if dumps else None,
        config=site_root / "wp-config.php",
        exclusion=INCLUDE_EVERYTHING,
        cipher=None,
        manifest_builder=_manifest_builder(scope, dump=dumps),
        workspace=workspace,
    )
    archive.write_bytes(out.getvalue())
    index = TarArchiveProbe().member_index(target)
    assert index.first_member == MANIFEST_MEMBER
    assert classify(index).format_version == FORMAT_VERSION
