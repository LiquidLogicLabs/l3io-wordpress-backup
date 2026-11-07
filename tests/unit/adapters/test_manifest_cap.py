"""BL-E004-053 (owner decision E004 D2 (c), 2026-11-07) — the Manifest cap.

``MANIFEST_MAX_BYTES`` is 256 MiB, and a backup whose Manifest would exceed it
is refused (``ManifestTooLargeError``, ``REFUSED``) after the read-only walk
and before anything is written: the Manifest is built from the collected
members with the same builder and measured with the same serialiser.

The cap is monkeypatched in ``adapters.tar`` (writer and reader read it there)
to the measured bound of a small Site, so "just over" and "just under" are
exact without writing 256 MiB. The real-size read (a Manifest just under
256 MiB) is not run here: its memory is stated from the measured ratio below.
"""

from __future__ import annotations

import io
import shutil
import tracemalloc
from datetime import UTC, datetime
from pathlib import Path

import pytest

from l3io.wp.backup.adapters import tar as tar_module
from l3io.wp.backup.adapters.tar import TarArchiveWriter, verify_archive
from l3io.wp.backup.cli.exit import exit_code_for
from l3io.wp.backup.domain.archive_format import (
    FORMAT_VERSION,
    MANIFEST_MAX_BYTES,
    PAYLOAD_MEMBER,
    ExcludedEntry,
    Manifest,
    ManifestExclusions,
    ManifestMember,
    ManifestPayload,
)
from l3io.wp.backup.domain.errors import ManifestTooLargeError
from l3io.wp.backup.domain.exclusion import INCLUDE_EVERYTHING
from l3io.wp.backup.domain.exit_codes import ExitCode

PROJECT_ROOT = Path(__file__).resolve().parents[3]
WP_CONFIG = PROJECT_ROOT / "tests" / "data" / "wp-config.php"
#: Peak traced memory reading a Manifest, as a multiple of its size, plus a
#: fixed allowance. Measured near 10x for this shape (many short members);
#: at the real 256 MiB cap that states a worst case of a few GiB, which is
#: why streaming the parse is a backlog item, not this story.
MEMORY_FACTOR = 25
MEMORY_ALLOWANCE = 16 * 1024 * 1024


def _builder(
    index: tuple[ManifestMember, ...], excluded: tuple[ExcludedEntry, ...]
) -> Manifest:
    return Manifest(
        format_version=FORMAT_VERSION,
        created_at=datetime.now(UTC).isoformat(),
        backup_scope="files",
        source={"site_root": "/var/www/site", "db_name": "wptest"},
        encrypted=False,
        payload=ManifestPayload(name=PAYLOAD_MEMBER, size=0, sha256="0" * 64),
        members=index,
        dump_present=False,
        exclusions=ManifestExclusions(source="none", patterns=(), excluded=excluded),
    )


def _site(tmp_path: Path, files: int) -> Path:
    site = tmp_path / "site"
    site.mkdir()
    shutil.copyfile(WP_CONFIG, site / "wp-config.php")
    content = site / "wp-content"
    content.mkdir()
    for n in range(files):
        (content / f"upload-{n:06d}-{'x' * 40}.txt").write_bytes(b"x")
    return site


def _write(site: Path, workspace: Path) -> tuple[io.BytesIO, Manifest]:
    out = io.BytesIO()
    manifest = TarArchiveWriter().write(
        out,
        site_root=site,
        dump=None,
        config=site / "wp-config.php",
        exclusion=INCLUDE_EVERYTHING,
        cipher=None,
        manifest_builder=_builder,
        workspace=workspace,
    )
    return out, manifest


def _bound(site: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    """The size the writer measures for ``site``: refused under a cap of 0."""
    monkeypatch.setattr(tar_module, "MANIFEST_MAX_BYTES", 0)
    with pytest.raises(ManifestTooLargeError) as caught:
        _write(site, workspace)
    return caught.value.size


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "workspace"
    path.mkdir(mode=0o700)
    return path


def test_the_cap_is_256_mib() -> None:
    assert MANIFEST_MAX_BYTES == 256 * 1024 * 1024
    assert tar_module.MANIFEST_MAX_BYTES == MANIFEST_MAX_BYTES


def test_just_over_the_cap_is_refused_with_nothing_written(
    tmp_path: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = _site(tmp_path, 50)
    bound = _bound(site, workspace, monkeypatch)
    monkeypatch.setattr(tar_module, "MANIFEST_MAX_BYTES", bound - 1)
    out = io.BytesIO()
    with pytest.raises(ManifestTooLargeError) as caught:
        TarArchiveWriter().write(
            out,
            site_root=site,
            dump=None,
            config=site / "wp-config.php",
            exclusion=INCLUDE_EVERYTHING,
            cipher=None,
            manifest_builder=_builder,
            workspace=workspace,
        )
    refused = caught.value
    assert (refused.size, refused.limit) == (bound, bound - 1)
    # 50 uploads, the Site Root and wp-content, its wp-config.php, and the
    # config member.
    assert refused.members == 50 + 2 + 1 + 1
    assert exit_code_for(refused) is ExitCode.REFUSED
    for field in (str(refused.members), str(refused.size), str(refused.limit)):
        assert field in str(refused)
    assert out.getvalue() == b""
    assert list(workspace.iterdir()) == []


def test_just_under_the_cap_is_written_and_verifies(
    tmp_path: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = _site(tmp_path, 50)
    bound = _bound(site, workspace, monkeypatch)
    monkeypatch.setattr(tar_module, "MANIFEST_MAX_BYTES", bound)
    out, manifest = _write(site, workspace)
    archive = tmp_path / "a.tar.gz"
    archive.write_bytes(out.getvalue())
    assert len(manifest.members) == 54
    assert verify_archive(archive).passed


def test_the_measured_size_is_an_upper_bound_of_the_written_manifest(
    tmp_path: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = _site(tmp_path, 20)
    bound = _bound(site, workspace, monkeypatch)
    monkeypatch.setattr(tar_module, "MANIFEST_MAX_BYTES", MANIFEST_MAX_BYTES)
    _, manifest = _write(site, workspace)
    written = len(tar_module.manifest_to_json(manifest).encode("utf-8"))
    assert written <= bound
    assert bound - written < 64, "the bound is an estimate, not the serialiser"


def test_reading_a_manifest_near_the_cap_stays_within_the_stated_bound(
    tmp_path: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = _site(tmp_path, 4000)
    bound = _bound(site, workspace, monkeypatch)
    monkeypatch.setattr(tar_module, "MANIFEST_MAX_BYTES", bound)
    out, _ = _write(site, workspace)
    archive = tmp_path / "a.tar.gz"
    archive.write_bytes(out.getvalue())
    tracemalloc.start()
    try:
        result = verify_archive(archive)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result.passed
    assert bound > 256 * 1024, "the Manifest is too small to measure anything"
    assert peak < MEMORY_FACTOR * bound + MEMORY_ALLOWANCE, (peak, bound)
