"""Owner decision 88 — the inner payload's gzip level: 6 by default, configurable.

The inner payload was written by ``tarfile``'s ``w|gz``, which always deflates
at level 9; the writer now deflates it at the ``compression_level`` it is
given (:data:`DEFAULT_COMPRESSION_LEVEL`, 6, unless set). The level is
identified from the bytes (``tests/payload_level.py``), not from the code.
Unchanged: the outer stream stays level 0 (decision 21), compression happens
before encryption, the gzip header names no path, and the readers take every
level.
"""

from __future__ import annotations

import gzip
import inspect
import io
import random
from collections.abc import Callable
from pathlib import Path, PurePosixPath

import pytest

from l3io.wp.backup.adapters.noop import NoopArchiveWriter
from l3io.wp.backup.adapters.tar import (
    TarArchiveWriter,
    collect_site_root,
    extract_archive,
    file_member,
    verify_archive_view,
    write_archive,
)
from l3io.wp.backup.domain.archive_format import (
    Manifest,
    ManifestMember,
    MemberRole,
)
from l3io.wp.backup.domain.compression import (
    COMPRESSION_LEVELS,
    DEFAULT_COMPRESSION_LEVEL,
)
from l3io.wp.backup.domain.exclusion import INCLUDE_EVERYTHING
from l3io.wp.backup.domain.storage_target import StorageTarget
from l3io.wp.backup.ports.archive import ArchiveWriterCommand
from tests.adapters.conftest import XorCipher
from tests.payload_level import matching_levels, payload_stream, xfl

Builder = Callable[[tuple[ManifestMember, ...]], Manifest]
GZIP_MAGIC = b"\x1f\x8b"


@pytest.fixture
def text_site(site_root: Path) -> Path:
    """``site_root`` plus ~256 KiB of varied text, where levels 1, 6, 9 differ."""
    words = random.Random(88).choices(  # noqa: S311 - test data, not security
        ["post", "meta", "wp", "content", "user", "the", "and", "option", "id"],
        k=48_000,
    )
    (site_root / "wp-content" / "post.html").write_text(
        " ".join(f"{w}{i % 97}" for i, w in enumerate(words)), encoding="utf-8"
    )
    return site_root


def _write(
    tmp_path: Path,
    site_root: Path,
    dump: Path,
    builder: Builder,
    workspace: Path,
    *,
    cipher: XorCipher | None = None,
    **level: int,
) -> Path:
    members = [
        file_member(MemberRole.DUMP, "database.sql", dump),
        file_member(MemberRole.CONFIG, "wp-config.php", site_root / "wp-config.php"),
        *collect_site_root(site_root, INCLUDE_EVERYTHING).members,
    ]
    out_path = tmp_path / "out" / "site.tar.gz"
    out_path.parent.mkdir(exist_ok=True)
    out_path.unlink(missing_ok=True)
    with out_path.open("wb") as out:
        write_archive(
            out, builder, members, workspace=workspace, cipher=cipher, **level
        )
    return out_path


def test_the_default_level_is_6() -> None:
    """Pinned: a refactor cannot fall back to an implicit 9 unnoticed."""
    assert DEFAULT_COMPRESSION_LEVEL == 6
    assert tuple(COMPRESSION_LEVELS) == (1, 2, 3, 4, 5, 6, 7, 8, 9)


def test_the_text_fixture_separates_the_levels(
    tmp_path: Path,
    text_site: Path,
    dump_file: Path,
    manifest_builder: Builder,
    workspace: Path,
) -> None:
    """The scope check: levels 1, 6 and 9 deflate this site differently."""
    raw = gzip.decompress(
        payload_stream(
            _write(tmp_path, text_site, dump_file, manifest_builder, workspace)
        )
    )
    bodies = {level: gzip.compress(raw, level, mtime=0) for level in (1, 6, 9)}
    assert len(set(bodies.values())) == 3


def test_the_writer_deflates_the_payload_at_level_6_by_default(
    tmp_path: Path,
    text_site: Path,
    dump_file: Path,
    manifest_builder: Builder,
    workspace: Path,
) -> None:
    stream = payload_stream(
        _write(tmp_path, text_site, dump_file, manifest_builder, workspace)
    )
    levels = matching_levels(stream)
    assert 6 in levels
    assert 9 not in levels, "the payload was deflated at Python's implicit 9"
    assert xfl(stream) == 0


@pytest.mark.parametrize(("level", "flag"), [(1, 4), (6, 0), (9, 2)])
def test_the_writer_deflates_the_payload_at_the_level_given(
    tmp_path: Path,
    text_site: Path,
    dump_file: Path,
    manifest_builder: Builder,
    workspace: Path,
    level: int,
    flag: int,
) -> None:
    stream = payload_stream(
        _write(
            tmp_path,
            text_site,
            dump_file,
            manifest_builder,
            workspace,
            compression_level=level,
        )
    )
    assert level in matching_levels(stream)
    assert {1, 6, 9} & matching_levels(stream) == {level}
    assert xfl(stream) == flag


@pytest.mark.parametrize("level", [1, 9])
def test_the_outer_stream_stays_level_0_whatever_the_payload_level(
    tmp_path: Path,
    text_site: Path,
    dump_file: Path,
    manifest_builder: Builder,
    workspace: Path,
    level: int,
) -> None:
    """Decision 21: the setting reaches the payload only."""
    data = _write(
        tmp_path,
        text_site,
        dump_file,
        manifest_builder,
        workspace,
        compression_level=level,
    ).read_bytes()
    assert data[:2] == GZIP_MAGIC
    # As test_outer_gzip.py checks level 0: stored blocks, a few bytes of
    # framing each (stored-block boundaries follow the writes, so the bytes
    # are not compared with a one-shot deflate).
    assert (data[10] >> 1) & 0b11 == 0, "the first deflate block is not stored"
    raw = gzip.decompress(data)
    assert len(raw) < len(data) <= len(raw) + 64 + 5 * (len(raw) // 65_535 + 1)


def test_the_payload_header_names_no_path(
    tmp_path: Path,
    text_site: Path,
    dump_file: Path,
    manifest_builder: Builder,
    workspace: Path,
) -> None:
    stream = payload_stream(
        _write(tmp_path, text_site, dump_file, manifest_builder, workspace)
    )
    assert stream[:3] == b"\x1f\x8b\x08"
    assert stream[3] == 0, "no file name, comment or extra field"


def test_compression_happens_before_encryption(
    tmp_path: Path,
    text_site: Path,
    dump_file: Path,
    manifest_builder: Builder,
    workspace: Path,
) -> None:
    """The cipher is handed the gzip stream at the level given, then encrypts it."""
    cipher = XorCipher()
    sealed = payload_stream(
        _write(
            tmp_path,
            text_site,
            dump_file,
            manifest_builder,
            workspace,
            cipher=cipher,
            compression_level=1,
        )
    )
    assert cipher.encrypt_calls == 1
    assert sealed[:2] != GZIP_MAGIC
    plain = io.BytesIO()
    cipher.decrypt(io.BytesIO(sealed), plain)
    assert plain.getvalue()[:2] == GZIP_MAGIC
    assert {1, 6, 9} & matching_levels(plain.getvalue()) == {1}


@pytest.mark.parametrize("level", [1, 6, 9])
def test_every_level_verifies_and_extracts_unchanged(
    tmp_path: Path,
    text_site: Path,
    dump_file: Path,
    manifest_builder: Builder,
    workspace: Path,
    level: int,
) -> None:
    """The readers detect gzip and need no level: nothing on the read side changed."""
    archive = _write(
        tmp_path,
        text_site,
        dump_file,
        manifest_builder,
        workspace,
        compression_level=level,
    )
    view, result = verify_archive_view(archive)
    assert result.passed, result.failures
    assert view is not None
    destination = tmp_path / f"restored-{level}"
    destination.mkdir()
    source = StorageTarget(scheme="file", path=PurePosixPath(archive))
    extract_archive(source, destination, view, roles=frozenset({MemberRole.SITE_ROOT}))
    for path in text_site.rglob("*"):
        if path.is_file():
            relative = path.relative_to(text_site)
            assert (destination / relative).read_bytes() == path.read_bytes()


@pytest.mark.parametrize("level", [0, 10, -1])
def test_write_archive_refuses_a_level_outside_1_to_9(
    tmp_path: Path,
    text_site: Path,
    dump_file: Path,
    manifest_builder: Builder,
    workspace: Path,
    level: int,
) -> None:
    from l3io.wp.backup.domain.errors import UsageError

    with pytest.raises(UsageError) as caught:
        _write(
            tmp_path,
            text_site,
            dump_file,
            manifest_builder,
            workspace,
            compression_level=level,
        )
    assert caught.value.arguments == ("compression_level",)
    assert list(workspace.iterdir()) == []


@pytest.mark.parametrize(
    "writer", [TarArchiveWriter, NoopArchiveWriter], ids=lambda w: w.__name__
)
def test_every_writer_takes_the_level_with_no_default(writer: type) -> None:
    """The port states it, so a writer cannot drop it and fall back silently."""
    for each in (ArchiveWriterCommand, writer):
        parameter = inspect.signature(each.write).parameters["compression_level"]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is inspect.Parameter.empty
