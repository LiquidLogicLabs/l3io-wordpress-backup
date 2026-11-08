"""AD-23/ADR-0024 — the tar probe, ``verify_archive``, ``read_member`` and
``decrypt_payload`` against real Archive bytes.

v1 Archives are the committed fixture or written by the real writer; v0
Archives are built in the test in both shapes 0.2.21 wrote (gzip and plain).
"""

from __future__ import annotations

import dataclasses
import errno
import hashlib
import io
import json
import os
import posixpath
import shutil
import stat
import tarfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
import structlog

from l3io.wp.backup.adapters import tar as tar_module
from l3io.wp.backup.adapters.tar import (
    ArchiveMemberSource,
    TarArchiveProbe,
    collect_site_root,
    decrypt_payload,
    extract_archive,
    file_member,
    verify_archive,
    verify_or_raise,
    write_archive,
)
from l3io.wp.backup.domain.archive_format import (
    CONFIG_RELATIVE,
    DUMP_RELATIVE,
    MANIFEST_MEMBER,
    PAYLOAD_MEMBER,
    V0_DUMP_MEMBER,
    V0_SITE_ROOT,
    ArchiveView,
    Integrity,
    Manifest,
    MemberRole,
    TarMemberType,
    VerificationReason,
    classify,
)
from l3io.wp.backup.domain.errors import (
    ArchiveNotFoundError,
    ArchiveReadError,
    ArchiveWriteError,
    MemberReadRefusedError,
    UnsupportedStorageSchemeError,
    VerificationFailedError,
)
from l3io.wp.backup.domain.exclusion import INCLUDE_EVERYTHING
from l3io.wp.backup.domain.storage_target import StorageTarget
from l3io.wp.backup.ports.crypto import PayloadCipher
from tests.archive_fixtures import archive_files
from tests.fixtures.archives.v0 import make_v0_fixtures

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "archives" / "v1" / "minimal.tar.gz"
WP_CONFIG = PROJECT_ROOT / "tests" / "data" / "wp-config.php"
V0_FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "archives" / "v0"
#: The committed passing v0 Archives (E004-S01-002), from the directory.
V0_PASSING = archive_files(V0_FIXTURES)
R = VerificationReason
PROBE = TarArchiveProbe()

Outer = list[tuple[str, bytes]]
Builder = Callable[..., Manifest]


def _target(path: Path) -> StorageTarget:
    return StorageTarget(scheme="file", path=PurePosixPath(path))


def _view(path: Path) -> ArchiveView:
    return classify(PROBE.member_index(_target(path)))


def _reasons(path: Path) -> list[tuple[str | None, VerificationReason]]:
    return [(f.member, f.reason) for f in verify_archive(path).failures]


def _fixture_parts() -> tuple[bytes, bytes]:
    with tarfile.open(FIXTURE) as outer:
        manifest = outer.extractfile(MANIFEST_MEMBER)
        payload = outer.extractfile(PAYLOAD_MEMBER)
        assert manifest is not None
        assert payload is not None
        return manifest.read(), payload.read()


def _tar(path: Path, members: list[tarfile.TarInfo | tuple[str, bytes]]) -> Path:
    with tarfile.open(path, mode="w") as out:
        for member in members:
            if isinstance(member, tarfile.TarInfo):
                out.addfile(member)
                continue
            name, data = member
            info = tarfile.TarInfo(name)
            info.size = len(data)
            out.addfile(info, io.BytesIO(data))
    return path


def _v0(path: Path, *, gz: bool, dump: bool = True, extra: Outer | None = None) -> Path:
    with tarfile.open(path, mode="w:gz" if gz else "w") as out:
        root = tarfile.TarInfo(V0_SITE_ROOT)
        root.type = tarfile.DIRTYPE
        out.addfile(root)
        members: Outer = [
            *([(V0_DUMP_MEMBER, b"CREATE TABLE t (id INT);\n")] if dump else []),
            (f"{V0_SITE_ROOT}/wp-config.php", WP_CONFIG.read_bytes()),
            (f"{V0_SITE_ROOT}/index.php", b"<?php\n"),
            *(extra or []),
        ]
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            out.addfile(info, io.BytesIO(data))
    return path


def _link(name: str, kind: bytes, target: str = "") -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = target
    return info


def _manifest_with(**changes: object) -> bytes:
    manifest, _ = _fixture_parts()
    document = json.loads(manifest)
    for key, value in changes.items():
        if callable(value):
            document[key] = value(document[key])
        else:
            document[key] = value
    return json.dumps(document).encode("utf-8")


# --------------------------------------------------------------------------
# Passing Archives
# --------------------------------------------------------------------------


def test_the_committed_fixture_verifies() -> None:
    result = verify_archive(FIXTURE)
    assert (result.passed, result.integrity, result.format_version) == (
        True,
        Integrity.VERIFIED,
        1,
    )
    verify_or_raise(FIXTURE)


@pytest.mark.parametrize("gz", [True, False], ids=["gzip", "plain"])
@pytest.mark.parametrize("dump", [True, False], ids=["with-dump", "files-only"])
def test_a_v0_archive_passes_as_structural_only(
    tmp_path: Path, *, gz: bool, dump: bool
) -> None:
    path = _v0(tmp_path / "old.tar.gz", gz=gz, dump=dump)
    result = verify_archive(path)
    assert (result.passed, result.integrity, result.format_version) == (
        True,
        Integrity.STRUCTURAL_ONLY,
        None,
    )
    view = _view(path)
    assert view.dump_present is dump
    assert (MemberRole.DUMP in {m.role for m in view.members}) is dump


@pytest.mark.parametrize("gz", [True, False], ids=["gzip", "plain"])
def test_the_publish_gate_refuses_a_v0_shaped_archive(
    tmp_path: Path, *, gz: bool
) -> None:
    """BL-E002-048: the writer only writes v1, so STRUCTURAL_ONLY never publishes."""
    path = _v0(tmp_path / "old.tar.gz", gz=gz, dump=True)
    assert verify_archive(path).passed
    with pytest.raises(VerificationFailedError) as caught:
        verify_or_raise(path)
    assert caught.value.result.passed is False
    assert caught.value.result.integrity is Integrity.STRUCTURAL_ONLY
    (failure,) = caught.value.result.failures
    assert failure.reason is VerificationReason.UNRECOGNISED_FORMAT


def test_the_probe_reports_raw_facts(tmp_path: Path) -> None:
    manifest, payload = _fixture_parts()
    index = PROBE.member_index(_target(FIXTURE))
    assert index.names == (MANIFEST_MEMBER, PAYLOAD_MEMBER)
    assert index.first_member == MANIFEST_MEMBER
    assert index.manifest_text == manifest.decode("utf-8")
    assert (index.readable_to_end, index.gzip) == (True, False)
    digests = PROBE.member_digests(_target(FIXTURE))
    assert digests[PAYLOAD_MEMBER].sha256 == hashlib.sha256(payload).hexdigest()
    assert digests[PAYLOAD_MEMBER].size == len(payload)
    assert digests[PAYLOAD_MEMBER].tar_type is TarMemberType.FILE
    v0 = PROBE.member_index(_target(_v0(tmp_path / "old.tar.gz", gz=True)))
    assert (v0.gzip, v0.manifest_text, v0.first_member) == (True, None, V0_SITE_ROOT)


# --------------------------------------------------------------------------
# Truncation and corruption
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda d: FIXTURE.read_bytes(), id="v1"),
        pytest.param(lambda d: _v0(d / "v0.tgz", gz=True).read_bytes(), id="v0-gzip"),
        pytest.param(lambda d: _v0(d / "v0.tar", gz=False).read_bytes(), id="v0-plain"),
    ],
)
def test_truncation_at_every_offset_is_truncated(
    tmp_path: Path, build: Callable[[Path], bytes]
) -> None:
    data = build(tmp_path)
    cut = tmp_path / "cut"
    wrong = []
    with structlog.testing.capture_logs():
        for offset in range(1, len(data)):
            cut.write_bytes(data[:offset])
            if _reasons(cut) != [(None, R.TRUNCATED)]:
                wrong.append(offset)
    assert not wrong, f"offsets not TRUNCATED: {wrong[:10]} ({len(wrong)})"


def test_every_single_byte_flip_in_the_payload_fails_naming_it(
    tmp_path: Path,
) -> None:
    data = bytearray(FIXTURE.read_bytes())
    with tarfile.open(FIXTURE) as outer:
        member = outer.getmember(PAYLOAD_MEMBER)
        start, size = member.offset_data, member.size
    flipped = tmp_path / "flipped"
    wrong = []
    with structlog.testing.capture_logs():
        for offset in range(start, start + size):
            data[offset] ^= 0xFF
            flipped.write_bytes(data)
            data[offset] ^= 0xFF
            if _reasons(flipped) != [(PAYLOAD_MEMBER, R.CHECKSUM_MISMATCH)]:
                wrong.append(offset)
    assert size > 0
    assert not wrong, f"flips not CHECKSUM_MISMATCH: {wrong[:10]}"


# --------------------------------------------------------------------------
# Structural failures, one per reason the outer tar can give
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("members", "expected"),
    [
        pytest.param(
            lambda m, p: [
                (MANIFEST_MEMBER, m),
                (PAYLOAD_MEMBER, p),
                _link("x", tarfile.LNKTYPE, PAYLOAD_MEMBER),
            ],
            [("x", R.UNDECLARED_MEMBER), ("x", R.UNSUPPORTED_MEMBER_TYPE)],
            id="hard-link",
        ),
        pytest.param(
            lambda m, p: [
                (MANIFEST_MEMBER, m),
                (PAYLOAD_MEMBER, p),
                _link("fifo", tarfile.FIFOTYPE),
            ],
            [("fifo", R.UNDECLARED_MEMBER), ("fifo", R.UNSUPPORTED_MEMBER_TYPE)],
            id="fifo",
        ),
        pytest.param(
            lambda m, p: [
                (MANIFEST_MEMBER, m),
                (PAYLOAD_MEMBER, p),
                (MANIFEST_MEMBER, m),
            ],
            [(MANIFEST_MEMBER, R.UNDECLARED_MEMBER)],
            id="second-manifest",
        ),
        pytest.param(
            lambda m, p: [(MANIFEST_MEMBER, m), (PAYLOAD_MEMBER, p), ("extra", b"")],
            [("extra", R.UNDECLARED_MEMBER)],
            id="third-member",
        ),
        pytest.param(
            lambda m, p: [(MANIFEST_MEMBER, m)],
            [(PAYLOAD_MEMBER, R.MEMBER_MISSING)],
            id="no-payload",
        ),
        pytest.param(
            lambda m, p: [(MANIFEST_MEMBER, m), _link(PAYLOAD_MEMBER, tarfile.DIRTYPE)],
            [(PAYLOAD_MEMBER, R.KIND_MISMATCH)],
            id="payload-not-a-file",
        ),
        pytest.param(
            lambda m, p: [(MANIFEST_MEMBER, m), (PAYLOAD_MEMBER, p + b"x")],
            [(PAYLOAD_MEMBER, R.SIZE_MISMATCH)],
            id="payload-size",
        ),
        pytest.param(
            lambda m, p: [(PAYLOAD_MEMBER, p), (MANIFEST_MEMBER, m)],
            [(PAYLOAD_MEMBER, R.UNRECOGNISED_FORMAT)],
            id="manifest-not-first",
        ),
        pytest.param(
            lambda m, p: [(MANIFEST_MEMBER, b"{not json"), (PAYLOAD_MEMBER, p)],
            [(MANIFEST_MEMBER, R.UNREADABLE)],
            id="manifest-not-json",
        ),
        pytest.param(
            lambda m, p: [(MANIFEST_MEMBER, b"\xff\xfe"), (PAYLOAD_MEMBER, p)],
            [(MANIFEST_MEMBER, R.UNREADABLE)],
            id="manifest-not-utf8",
        ),
        pytest.param(
            lambda m, p: [
                (MANIFEST_MEMBER, _manifest_with(format_version=2)),
                (PAYLOAD_MEMBER, p),
            ],
            [("2", R.UNSUPPORTED_VERSION)],
            id="unsupported-version",
        ),
        pytest.param(
            lambda m, p: [
                (MANIFEST_MEMBER, _manifest_with(members=_dot_dot)),
                (PAYLOAD_MEMBER, p),
            ],
            [("site-root/../escape.php", R.UNSAFE_MEMBER_PATH)],
            id="index-dot-dot",
        ),
        pytest.param(
            lambda m, p: [
                (MANIFEST_MEMBER, _manifest_with(members=_no_dump)),
                (PAYLOAD_MEMBER, p),
            ],
            [(None, R.DUMP_DECLARED_ABSENT)],
            id="dump-declared-absent",
        ),
    ],
)
def test_a_crafted_outer_tar_fails_with_its_reason(
    tmp_path: Path,
    members: Callable[[bytes, bytes], list[Any]],
    expected: list[tuple[str | None, VerificationReason]],
) -> None:
    manifest, payload = _fixture_parts()
    path = _tar(tmp_path / "crafted.tar", members(manifest, payload))
    assert _reasons(path) == expected
    with pytest.raises(VerificationFailedError) as caught:
        verify_or_raise(path)
    assert caught.value.result.passed is False


def _dot_dot(members: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entry = {**members[-1], "path": "site-root/../escape.php", "kind": "file"}
    entry["link_target"] = None
    return [*members, entry]


def _no_dump(members: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [m for m in members if m["role"] != "dump"]


@pytest.mark.parametrize(
    "text",
    [b"[" * 200_000, b'{"format_version": 1' + b"0" * 5000 + b"}"],
    ids=["deeply-nested", "huge-integer"],
)
def test_a_hostile_manifest_is_unreadable_not_a_crash(
    tmp_path: Path, text: bytes
) -> None:
    _, payload = _fixture_parts()
    path = _tar(
        tmp_path / "hostile.tar", [(MANIFEST_MEMBER, text), (PAYLOAD_MEMBER, payload)]
    )
    assert _reasons(path) == [(MANIFEST_MEMBER, R.UNREADABLE)]


def _end_of_members() -> int:
    with tarfile.open(FIXTURE) as outer:
        member = outer.getmember(PAYLOAD_MEMBER)
    blocks = -(-member.size // tarfile.BLOCKSIZE)
    return member.offset_data + blocks * tarfile.BLOCKSIZE


@pytest.mark.parametrize(
    "corruption", ["hidden-header", "end-block-flip", "garbage-after"]
)
def test_corruption_after_the_last_member_is_not_verified(
    tmp_path: Path, corruption: str
) -> None:
    """tarfile stops silently at a bad header after the first; the verifier must not."""
    data = bytearray(FIXTURE.read_bytes())
    end = _end_of_members()
    if corruption == "hidden-header":
        header = bytearray(tarfile.TarInfo("hidden").tobuf())
        header[148] ^= 0x01  # the checksum field: tarfile now ignores the block
        data[end : end + tarfile.BLOCKSIZE] = header
    elif corruption == "end-block-flip":
        data[end + 7] ^= 0xFF
    else:
        data += b"\x01" + bytes(tarfile.RECORDSIZE - 1)
    path = tmp_path / "corrupted.tar"
    path.write_bytes(data)
    assert _reasons(path) == [(None, R.TRUNCATED)]


def test_a_manifest_over_the_cap_is_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tar_module, "MANIFEST_MAX_BYTES", 16)
    assert _reasons(FIXTURE) == [(MANIFEST_MEMBER, R.UNREADABLE)]


@pytest.mark.parametrize(
    ("extra", "reason"),
    [
        (
            [_link(f"{V0_SITE_ROOT}/x", tarfile.LNKTYPE, f"{V0_SITE_ROOT}/index.php")],
            R.UNSUPPORTED_MEMBER_TYPE,
        ),
        ([_link(f"{V0_SITE_ROOT}/fifo", tarfile.FIFOTYPE)], R.UNSUPPORTED_MEMBER_TYPE),
        ([(f"{V0_SITE_ROOT}/../x", b"")], R.UNSAFE_MEMBER_PATH),
        ([(MANIFEST_MEMBER, b"{}")], R.UNRECOGNISED_FORMAT),
    ],
)
def test_a_crafted_v0_archive_fails(
    tmp_path: Path, extra: list[Any], reason: VerificationReason
) -> None:
    path = tmp_path / "old.tar"
    with tarfile.open(path, mode="w") as out:
        for member in [(V0_DUMP_MEMBER, b"SQL"), (f"{V0_SITE_ROOT}/a", b"a"), *extra]:
            if isinstance(member, tarfile.TarInfo):
                out.addfile(member)
                continue
            info = tarfile.TarInfo(member[0])
            info.size = len(member[1])
            out.addfile(info, io.BytesIO(member[1]))
    assert reason in {r for _, r in _reasons(path)}


def test_a_dump_without_a_site_root_is_unrecognised(tmp_path: Path) -> None:
    path = _tar(tmp_path / "dump-only.tar", [(V0_DUMP_MEMBER, b"SQL")])
    assert _reasons(path) == [(V0_DUMP_MEMBER, R.UNRECOGNISED_FORMAT)]


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"\x00" * 10 + b"not a tar at all\n",
        b"PK\x03\x04" + b"\x00" * 600,
        b"x" * 4000,
    ],
    ids=["empty", "junk", "zip", "text"],
)
def test_a_non_archive_is_unrecognised(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "not-an-archive"
    path.write_bytes(content)
    assert _reasons(path) == [(None, R.UNRECOGNISED_FORMAT)]


@pytest.mark.parametrize("make", ["missing", "directory"])
def test_a_missing_archive_is_archive_not_found(tmp_path: Path, make: str) -> None:
    path = tmp_path / "nowhere.tar.gz"
    if make == "directory":
        path.mkdir()
    with pytest.raises(ArchiveNotFoundError) as caught:
        verify_archive(path)
    assert isinstance(caught.value.__cause__, OSError)


def test_a_non_file_storage_target_is_refused() -> None:
    target = StorageTarget(scheme="s3", path=PurePosixPath("/bucket/a"))  # type: ignore[arg-type]
    with pytest.raises(UnsupportedStorageSchemeError):
        PROBE.member_index(target)


def _snapshot(*roots: Path) -> dict[str, tuple[int, int, int]]:
    found = {}
    for root in roots:
        for path in [root, *sorted(root.rglob("*"))]:
            info = path.lstat()
            found[str(path)] = (info.st_mode, info.st_size, info.st_mtime_ns)
    return found


def test_verification_writes_nothing(tmp_path: Path) -> None:
    archives = tmp_path / "archives"
    archives.mkdir()
    archive = archives / "site.tar.gz"
    shutil.copyfile(FIXTURE, archive)
    site = tmp_path / "site"
    site.mkdir()
    (site / "wp-config.php").write_bytes(WP_CONFIG.read_bytes())
    before = _snapshot(tmp_path)
    assert verify_archive(archive).passed
    assert verify_archive(_v0(archives / "old.tgz", gz=True)).passed
    before[str(archives / "old.tgz")] = _snapshot(archives / "old.tgz")[
        str(archives / "old.tgz")
    ]
    before[str(archives)] = _snapshot(archives)[str(archives)]
    assert _snapshot(tmp_path) == before


def test_verification_emits_one_verify_record(tmp_path: Path) -> None:
    with structlog.testing.capture_logs() as logs:
        verify_archive(FIXTURE)
        verify_archive(_v0(tmp_path / "old.tgz", gz=True, dump=False))
        bad = _tar(tmp_path / "bad.tar", [(PAYLOAD_MEMBER, b"x")])
        verify_archive(bad)
    records = [e for e in logs if e["event"] == "phase_completed"]
    assert [r["phase"] for r in records] == ["VERIFY"] * 3
    good, old, failed = records
    assert good["bytes"] == FIXTURE.stat().st_size
    assert (good["format_version"], good["integrity"], good["passed"]) == (
        1,
        "VERIFIED",
        True,
    )
    assert good["dump_present"] is True
    assert "reason" not in good
    assert (old["format_version"], old["integrity"], old["dump_present"]) == (
        "v0",
        "STRUCTURAL_ONLY",
        False,
    )
    assert (failed["passed"], failed["reason"], failed["member"]) == (
        False,
        "UNRECOGNISED_FORMAT",
        repr(PAYLOAD_MEMBER),
    )
    # Never classified, so no format was read: not "v0".
    assert (failed["format_version"], failed["integrity"]) == (None, None)


# --------------------------------------------------------------------------
# read_member, extraction from the outer tar, and decrypt_payload
# --------------------------------------------------------------------------


def test_read_member_reads_the_config(tmp_path: Path) -> None:
    view = _view(FIXTURE)
    config = f"{MemberRole.CONFIG.value}/{CONFIG_RELATIVE}"
    data = PROBE.read_member(_target(FIXTURE), view, config, max_bytes=1 << 20)
    assert data == WP_CONFIG.read_bytes()
    old = _v0(tmp_path / "old.tgz", gz=True)
    v0 = PROBE.read_member(
        _target(old), _view(old), "wp-root/wp-config.php", max_bytes=1 << 20
    )
    assert v0 == WP_CONFIG.read_bytes()


@pytest.mark.parametrize(
    ("path", "max_bytes", "match"),
    [
        ("site-root/wp-content", 1 << 20, "regular file"),
        ("site-root/latest.php", 1 << 20, "regular file"),
        ("site-root/missing.php", 1 << 20, "member index"),
        ("config/wp-config.php", 8, "cap"),
    ],
)
def test_read_member_refuses(path: str, max_bytes: int, match: str) -> None:
    with pytest.raises(MemberReadRefusedError, match=match):
        PROBE.read_member(_target(FIXTURE), _view(FIXTURE), path, max_bytes=max_bytes)


def test_extract_reads_the_payload_inside_the_outer_tar(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    result = extract_archive(
        _target(FIXTURE), out, _view(FIXTURE), roles=frozenset({MemberRole.SITE_ROOT})
    )
    assert (out / "wp-config.php").read_bytes() == WP_CONFIG.read_bytes()
    assert (out / "latest.php").readlink() == Path("index.php")
    assert (out / "wp-content" / "empty").is_dir()
    assert (out / "wp-content" / "b.txt").read_bytes() == b"linked\n"
    assert result.skipped_links == ()


def _tree(root: Path) -> dict[str, bytes | str | None]:
    found: dict[str, bytes | str | None] = {}
    for path in sorted(root.rglob("*")):
        key = path.relative_to(root).as_posix()
        if path.is_symlink():
            found[key] = f"-> {path.readlink()}"
        else:
            found[key] = path.read_bytes() if path.is_file() else None
    return found


def _v0_members(archive: Path) -> dict[str, bytes | str | None]:
    """What a v0 Archive holds, keyed as extraction lays it out."""
    found: dict[str, bytes | str | None] = {}
    with tarfile.open(archive) as tar:
        for member in tar:
            if member.name == V0_SITE_ROOT:
                continue
            key = (
                DUMP_RELATIVE
                if member.name == V0_DUMP_MEMBER
                else member.name.removeprefix(f"{V0_SITE_ROOT}/")
            )
            if member.issym():
                found[key] = f"-> {member.linkname}"
            elif member.isfile():
                handle = tar.extractfile(member)
                assert handle is not None
                found[key] = handle.read()
            else:
                found[key] = None
    return found


@pytest.mark.parametrize("archive", V0_PASSING, ids=lambda p: p.name)
def test_extract_reads_a_v0_archive_itself(tmp_path: Path, archive: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    result = extract_archive(
        _target(archive),
        out,
        _view(archive),
        roles=frozenset({MemberRole.SITE_ROOT, MemberRole.DUMP}),
    )
    members = _v0_members(archive)
    outward = {
        key
        for key, value in members.items()
        if isinstance(value, str)
        and (
            value.startswith("-> /")
            or posixpath.normpath(
                posixpath.join(posixpath.dirname(key), value.removeprefix("-> "))
            ).startswith("..")
        )
    }
    assert {s.path for s in result.skipped_links} == outward
    expected = {k: v for k, v in members.items() if k not in outward}
    assert _tree(out) == expected
    assert "wp-config.php" in expected


def test_the_v0_fixture_set_is_not_empty() -> None:
    assert len(V0_PASSING) >= 2, f"no passing v0 fixtures in {V0_FIXTURES}"


def test_extracting_the_pax_fixture_gives_back_the_generator_site(
    tmp_path: Path,
) -> None:
    site = tmp_path / "site"
    site.mkdir()
    make_v0_fixtures.make_site(site)
    archive = V0_FIXTURES / "synthetic-all-pax.tar.gz"
    out = tmp_path / "out"
    out.mkdir()
    extract_archive(
        _target(archive), out, _view(archive), roles=frozenset({MemberRole.SITE_ROOT})
    )
    assert _tree(out) == _tree(site)


def _encrypted(
    tmp_path: Path,
    site_root: Path,
    cipher: PayloadCipher,
    builder: Callable[..., Manifest],
) -> Path:
    dump = tmp_path / "dump.sql"
    dump.write_bytes(b"CREATE TABLE t (id INT);\n")
    workspace = tmp_path / "write-workspace"
    workspace.mkdir()
    members: list[ArchiveMemberSource] = [
        file_member(MemberRole.DUMP, DUMP_RELATIVE, dump),
        *collect_site_root(site_root, INCLUDE_EVERYTHING).members,
    ]
    path = tmp_path / "sealed.tar.gz"
    with path.open("wb") as out:
        manifest: Manifest = write_archive(
            out, builder, members, workspace=workspace, cipher=cipher
        )
    assert manifest.encrypted
    return path


def test_decrypt_payload_then_extract_and_read(
    tmp_path: Path, site_root: Path, cipher: PayloadCipher, manifest_builder: Builder
) -> None:
    sealed = _encrypted(tmp_path, site_root, cipher, manifest_builder)
    assert verify_archive(sealed).passed  # keyless (ADR-0024)
    view = _view(sealed)
    run = tmp_path / "run"
    run.mkdir(mode=0o700)
    plain = decrypt_payload(
        _target(sealed), view, cipher=cipher, destination=run / PAYLOAD_MEMBER
    )
    assert plain.parent == run
    assert stat.S_IMODE(plain.stat().st_mode) == 0o600
    out = tmp_path / "out"
    out.mkdir()
    extract_archive(
        _target(sealed),
        out,
        view,
        roles=frozenset({MemberRole.SITE_ROOT}),
        payload=plain,
    )
    assert (out / "index.php").read_bytes() == (site_root / "index.php").read_bytes()
    data = PROBE.read_member(
        _target(sealed), view, "dump/database.sql", max_bytes=1024, payload=plain
    )
    assert data == b"CREATE TABLE t (id INT);\n"


def test_a_failing_cipher_leaves_no_file(
    tmp_path: Path,
    site_root: Path,
    cipher_factory: Callable[..., PayloadCipher],
    manifest_builder: Builder,
) -> None:
    sealed = _encrypted(tmp_path, site_root, cipher_factory(), manifest_builder)
    destination = tmp_path / "plain.tar.gz"
    with pytest.raises(OSError, match="cipher failed"):
        decrypt_payload(
            _target(sealed),
            _view(sealed),
            cipher=cipher_factory(fail_after=1),
            destination=destination,
        )
    assert not destination.exists()


def test_an_encrypted_view_needs_payload(
    tmp_path: Path, site_root: Path, cipher: PayloadCipher, manifest_builder: Builder
) -> None:
    sealed = _encrypted(tmp_path, site_root, cipher, manifest_builder)
    view = _view(sealed)
    with pytest.raises(ValueError, match="payload="):
        extract_archive(
            _target(sealed), tmp_path, view, roles=frozenset({MemberRole.SITE_ROOT})
        )
    with pytest.raises(ValueError, match="payload="):
        PROBE.read_member(_target(sealed), view, "dump/database.sql", max_bytes=1024)


def test_decrypting_a_plain_archive_is_a_programming_error(
    tmp_path: Path, cipher: PayloadCipher
) -> None:
    with pytest.raises(ValueError, match="encrypted"):
        decrypt_payload(
            _target(FIXTURE),
            _view(FIXTURE),
            cipher=cipher,
            destination=tmp_path / "plain",
        )
    assert not (tmp_path / "plain").exists()


def test_an_unknown_manifest_value_is_logged_as_a_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BL-E002-034: a value the read dropped (``origin.kind``) is a WARNING."""
    real = tar_module.verify_probe

    def probe(*args: Any) -> Any:  # noqa: ANN401
        view, result = real(*args)
        assert view is not None
        unknown = (("origin.kind", "scheduled_rotation"),)
        return dataclasses.replace(view, unknown_values=unknown), result

    monkeypatch.setattr(tar_module, "verify_probe", probe)
    with structlog.testing.capture_logs() as logs:
        tar_module.verify_archive_manifest(FIXTURE)
    (record,) = [r for r in logs if r["event"] == "manifest_value_unknown"]
    assert record["log_level"] == "warning"
    assert record["field"] == "origin.kind"
    assert "scheduled_rotation" in record["value"]


def test_a_known_manifest_logs_no_unknown_value() -> None:
    with structlog.testing.capture_logs() as logs:
        view, _, _ = tar_module.verify_archive_manifest(FIXTURE)
    assert view is not None
    assert view.unknown_values == ()
    assert not [r for r in logs if r["event"] == "manifest_value_unknown"]


class _FailingWrites(io.FileIO):
    """The destination's descriptor, refusing every write with ``ENOSPC``."""

    def write(self, data: object, /) -> int:
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))


def test_a_destination_write_failure_is_an_archive_write_error(
    tmp_path: Path,
    site_root: Path,
    cipher: PayloadCipher,
    manifest_builder: Builder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BL-E002-049: no raw ``OSError`` escapes ``decrypt_payload``."""
    sealed = _encrypted(tmp_path, site_root, cipher, manifest_builder)
    destination = tmp_path / "plain.tar.gz"

    def failing_fdopen(fd: int, mode: str) -> io.BufferedWriter:
        return io.BufferedWriter(_FailingWrites(fd, mode))

    monkeypatch.setattr(tar_module.os, "fdopen", failing_fdopen)
    with pytest.raises(ArchiveWriteError) as caught:
        decrypt_payload(
            _target(sealed), _view(sealed), cipher=cipher, destination=destination
        )
    assert caught.value.path == destination
    assert not destination.exists()


def test_an_archive_read_failure_is_an_archive_read_error(
    tmp_path: Path,
    site_root: Path,
    cipher: PayloadCipher,
    manifest_builder: Builder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sealed = _encrypted(tmp_path, site_root, cipher, manifest_builder)
    destination = tmp_path / "plain.tar.gz"
    real_body = tar_module._payload_body  # noqa: SLF001

    class _Eio(io.RawIOBase):
        def readable(self) -> bool:
            return True

        def readinto(self, buffer: object, /) -> int:
            raise OSError(errno.EIO, os.strerror(errno.EIO))

    def failing_body(*args: Any) -> Any:  # noqa: ANN401
        real_body(*args)
        return _Eio()

    monkeypatch.setattr(tar_module, "_payload_body", failing_body)
    with pytest.raises(ArchiveReadError) as caught:
        decrypt_payload(
            _target(sealed), _view(sealed), cipher=cipher, destination=destination
        )
    assert caught.value.path == sealed
    assert not destination.exists()


def test_an_unopenable_payload_is_a_read_error_not_a_write_error(
    tmp_path: Path, site_root: Path, cipher: PayloadCipher, manifest_builder: Builder
) -> None:
    sealed = _encrypted(tmp_path, site_root, cipher, manifest_builder)
    missing = tmp_path / "no-such-payload.tar.gz"
    with pytest.raises(ArchiveReadError) as caught:
        extract_archive(
            _target(sealed),
            tmp_path / "out",
            _view(sealed),
            roles=frozenset({MemberRole.SITE_ROOT}),
            payload=missing,
        )
    assert caught.value.path == missing


def test_read_member_refuses_a_repeated_name(tmp_path: Path) -> None:
    """BL-E002-050: extraction would see the last copy, so neither is read."""
    config = f"{V0_SITE_ROOT}/wp-config.php"
    path = _v0(tmp_path / "twice.tar.gz", gz=True, extra=[(config, b"<?php // 2\n")])
    with pytest.raises(MemberReadRefusedError, match="repeated"):
        PROBE.read_member(_target(path), _view(path), config, max_bytes=1 << 20)


def test_read_member_still_reads_a_single_copy(tmp_path: Path) -> None:
    path = _v0(tmp_path / "once.tar.gz", gz=True)
    config = f"{V0_SITE_ROOT}/wp-config.php"
    data = PROBE.read_member(_target(path), _view(path), config, max_bytes=1 << 20)
    assert data == WP_CONFIG.read_bytes()
