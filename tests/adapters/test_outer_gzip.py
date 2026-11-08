"""BL-E002-032 (owner decision 21) — the outer tar is a gzip stream at level 0.

New Archives wrap the outer tar (Manifest first, then the payload) in a gzip
stream at compression level 0, so the ``.tar.gz`` name is true. Readers detect
the gzip magic and still accept a plain-tar v1 Archive: every Archive written
before the decision, and the committed fixtures, which stay plain.
"""

from __future__ import annotations

import gzip
import shutil
import tarfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath

import pytest

from l3io.wp.backup.adapters.tar import (
    OUTER_COMPRESSLEVEL,
    collect_site_root,
    extract_archive,
    file_member,
    verify_archive_view,
    write_archive,
)
from l3io.wp.backup.domain.archive_format import (
    FORMAT_VERSION,
    MANIFEST_MEMBER,
    PAYLOAD_MEMBER,
    Integrity,
    Manifest,
    ManifestMember,
    MemberRole,
)
from l3io.wp.backup.domain.exclusion import INCLUDE_EVERYTHING
from l3io.wp.backup.domain.storage_target import StorageTarget

PROJECT_ROOT = Path(__file__).resolve().parents[2]
V1_FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "archives" / "v1"
GZIP_MAGIC = b"\x1f\x8b"

Builder = Callable[[tuple[ManifestMember, ...]], Manifest]


def _write(
    tmp_path: Path, site_root: Path, dump: Path, builder: Builder, workspace: Path
) -> Path:
    members = [
        file_member(MemberRole.DUMP, "database.sql", dump),
        file_member(MemberRole.CONFIG, "wp-config.php", site_root / "wp-config.php"),
        *collect_site_root(site_root, INCLUDE_EVERYTHING).members,
    ]
    out_path = tmp_path / "out" / "site.tar.gz"
    out_path.parent.mkdir()
    with out_path.open("wb") as out:
        write_archive(out, builder, members, workspace=workspace, cipher=None)
    return out_path


@pytest.fixture
def archive(
    tmp_path: Path,
    site_root: Path,
    dump_file: Path,
    manifest_builder: Builder,
    workspace: Path,
) -> Path:
    return _write(tmp_path, site_root, dump_file, manifest_builder, workspace)


def _plain_copy(archive: Path, tmp_path: Path) -> Path:
    """The same outer tar without the gzip wrapper: a pre-decision v1 Archive."""
    plain = tmp_path / "plain" / "site.tar.gz"
    plain.parent.mkdir()
    with gzip.open(archive, "rb") as source, plain.open("wb") as out:
        shutil.copyfileobj(source, out)
    return plain


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_a_new_archive_starts_with_the_gzip_magic(archive: Path) -> None:
    assert archive.read_bytes()[:2] == GZIP_MAGIC


def test_a_new_archive_opens_with_tarfile_r_gz(archive: Path) -> None:
    """``tar xzf`` and MIME sniffing agree with the name: it is a gzip tar."""
    with tarfile.open(archive, mode="r:gz") as outer:
        assert outer.getnames() == [MANIFEST_MEMBER, PAYLOAD_MEMBER]


def test_the_writer_uses_compression_level_0(archive: Path) -> None:
    """Level 0: every deflate block is stored (BTYPE 00), nothing compressed."""
    assert OUTER_COMPRESSLEVEL == 0
    data = archive.read_bytes()
    flags = data[3]
    assert flags == 0, "the gzip header names no file and has no extra fields"
    # The first deflate block header follows the 10-byte gzip header.
    assert (data[10] >> 1) & 0b11 == 0, "the first deflate block is not stored"
    # Stored blocks: the stream is the tar plus a few bytes of framing per block.
    raw = gzip.decompress(data)
    assert len(raw) < len(data) <= len(raw) + 64 + 5 * (len(raw) // 65_535 + 1)


def test_the_gzip_header_carries_the_run_clock_and_no_path(archive: Path) -> None:
    mtime = int.from_bytes(archive.read_bytes()[4:8], "little")
    with tarfile.open(archive, mode="r:gz") as outer:
        assert outer.getmembers()[0].mtime == mtime


@pytest.mark.parametrize("encoding", ["gzip", "plain"])
def test_both_encodings_verify_and_extract_the_same_tree(
    archive: Path, tmp_path: Path, encoding: str
) -> None:
    path = archive if encoding == "gzip" else _plain_copy(archive, tmp_path)
    assert (path.read_bytes()[:2] == GZIP_MAGIC) is (encoding == "gzip")
    view, result = verify_archive_view(path)
    assert result.passed, result.failures
    assert view is not None
    assert view.format_version == FORMAT_VERSION
    assert result.integrity is Integrity.VERIFIED
    destination = tmp_path / f"restored-{encoding}"
    destination.mkdir()
    source = StorageTarget(scheme="file", path=PurePosixPath(path))
    extract_archive(source, destination, view, roles=frozenset({MemberRole.SITE_ROOT}))
    assert _tree(destination) == _tree(tmp_path / "site")


def test_a_cut_gzip_archive_fails_verification(archive: Path, tmp_path: Path) -> None:
    cut = tmp_path / "cut.tar.gz"
    cut.write_bytes(archive.read_bytes()[:-9])
    _, result = verify_archive_view(cut)
    assert not result.passed


@pytest.mark.parametrize(
    "name", ["minimal.tar.gz", "encrypted-passphrase.tar.gz"], ids=["plain", "enc"]
)
def test_the_committed_v1_fixtures_stay_plain_tar_and_verify(name: str) -> None:
    """Backward compatibility: the fixtures predate the wrapper and must still read."""
    fixture = V1_FIXTURES / name
    assert fixture.read_bytes()[:2] != GZIP_MAGIC
    view, result = verify_archive_view(fixture)
    assert result.passed, result.failures
    assert view is not None
    assert view.format_version == FORMAT_VERSION


def test_the_committed_plain_fixture_still_extracts(tmp_path: Path) -> None:
    fixture = V1_FIXTURES / "minimal.tar.gz"
    view, result = verify_archive_view(fixture)
    assert result.passed
    assert view is not None
    source = StorageTarget(scheme="file", path=PurePosixPath(fixture))
    extracted = extract_archive(
        source,
        tmp_path,
        view,
        roles=frozenset({MemberRole.SITE_ROOT, MemberRole.DUMP}),
    )
    assert extracted.members > 0
    assert (tmp_path / "wp-config.php").is_file()
