"""FR-5 — an exclusion set end to end, through the backup driver.

A files-only backup reads no database (``test_backup_scope.py`` runs it with
an unresolvable ``DB_HOST``), so this needs no live server and is unmarked; it
needs the ``database`` extra only because ``real_adapters`` builds the
database adapter. The Archive is written **outside** the Site Root:
``cli/backup.py::_SkipStaged`` records one extra ``excluded`` entry for a
target inside it.
"""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

pytest.importorskip("l3io.wp.database")

from l3io.wp.backup.adapters.exclusions import LoggedExclusionPolicy
from l3io.wp.backup.cli import backup as backup_cli
from l3io.wp.backup.core.exclusions import ExclusionSet
from l3io.wp.backup.domain.archive_format import (
    MANIFEST_MEMBER,
    PAYLOAD_MEMBER,
    Manifest,
    MemberRole,
    manifest_from_json,
)
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.exclusion import ExcludedEntry
from tests.conftest import CredentialSentinel

SITE_ROOT = MemberRole.SITE_ROOT.value


def _seed(site: Path) -> None:
    (site / "wp-content" / "cache").mkdir(parents=True)
    (site / "wp-content" / "cache" / "x").write_text("cached\n", encoding="utf-8")
    (site / "wp-content" / "debug.log").write_text("log\n", encoding="utf-8")
    (site / "wp-content" / "uploads").mkdir()
    (site / "wp-content" / "uploads" / "a.jpg").write_bytes(b"\xff\xd8jpeg")


def _read(final: Path) -> tuple[Manifest, set[str]]:
    with tarfile.open(final, mode="r:") as outer:
        manifest_file = outer.extractfile(MANIFEST_MEMBER)
        payload_file = outer.extractfile(PAYLOAD_MEMBER)
        assert manifest_file is not None
        assert payload_file is not None
        manifest = manifest_from_json(manifest_file.read().decode("utf-8"))
        with tarfile.open(fileobj=payload_file, mode="r|gz") as inner:
            names = {info.name for info in inner}
    return manifest, names


CASES = {
    "default": (
        ExclusionSet.default(),
        "default",
        (
            ExcludedEntry("wp-content/cache", "wp-content/cache/", is_dir=True),
            ExcludedEntry("wp-content/debug.log", "wp-content/debug.log", is_dir=False),
        ),
    ),
    "star": (
        ExclusionSet.from_patterns(["*"], include_defaults=True),
        "custom",
        (
            # Top-most paths only: ``*`` prunes ``wp-content`` before the
            # traversal reaches any default's target; ``index.php`` too.
            ExcludedEntry("index.php", "*", is_dir=False),
            ExcludedEntry("wp-content", "*", is_dir=True),
        ),
    ),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_a_backup_leaves_out_what_its_set_excludes(
    credential_sentinel: CredentialSentinel, tmp_path: Path, case: str
) -> None:
    policy, source, expected = CASES[case]
    site = credential_sentinel.site
    _seed(site)
    out = tmp_path / "out"
    out.mkdir()
    final = out / f"{case}.tar.gz"
    request = backup_cli.BackupRequest(
        site_root=site,
        scope=BackupScope.FILES,
        target=backup_cli.storage_target_from_cli(str(final)),
        wp_config=None,
        credentials=None,
        cipher=None,
        exclusion=LoggedExclusionPolicy(policy),
    )
    result = backup_cli.drive_backup(request, ctx=None)
    assert result.performed

    manifest, names = _read(final)
    assert f"{SITE_ROOT}/wp-config.php" in names
    assert f"{SITE_ROOT}/wp-content/cache/x" not in names
    assert f"{SITE_ROOT}/wp-content/cache" not in names
    assert f"{SITE_ROOT}/wp-content/debug.log" not in names
    uploaded = f"{SITE_ROOT}/wp-content/uploads/a.jpg"
    assert (uploaded in names) is (case == "default")

    assert manifest.exclusions is not None
    assert manifest.exclusions.source == source
    assert manifest.exclusions.patterns == policy.patterns
    assert manifest.exclusions.excluded == expected

    events = [e["event"] for e in credential_sentinel.raw]
    assert events.count("exclusions_applied") == 1
    assert events.count("path_excluded") == len(expected)
    kept = [e for e in credential_sentinel.raw if e["event"] == "protected_path_kept"]
    assert [e["pattern"] for e in kept] == (["*"] if case == "star" else [])
