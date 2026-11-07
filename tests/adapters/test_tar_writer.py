"""ADR-0024/ADR-0017 — the Archive writer and the publish gate."""

from __future__ import annotations

import dataclasses
import gzip
import hashlib
import io
import json
import os
import tarfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from l3io.wp.backup.adapters.tar import (
    ArchiveMemberSource,
    collect_site_root,
    file_member,
    verify_or_raise,
    write_archive,
)
from l3io.wp.backup.domain.archive_format import (
    ENCRYPTED_PAYLOAD_MEMBER,
    INNER_MANIFEST_MEMBER,
    MANIFEST_MEMBER,
    PAYLOAD_MEMBER,
    ConfigLocation,
    Manifest,
    ManifestMember,
    MemberKind,
    MemberRole,
    VerificationReason,
    check_inner_manifest,
    manifest_to_json,
    member_path,
)
from l3io.wp.backup.domain.errors import ArchiveWriteError, VerificationFailedError
from l3io.wp.backup.domain.exclusion import INCLUDE_EVERYTHING

Builder = Callable[..., Manifest]
Mutation = Callable[[bytes, bytes], list[tuple[str, bytes]]]


def _members(
    site_root: Path, dump: Path, config: Path | None = None
) -> list[ArchiveMemberSource]:
    collection = collect_site_root(site_root, INCLUDE_EVERYTHING)
    return [
        # Deliberately out of order: the writer puts the Dump first.
        *collection.members,
        file_member(MemberRole.CONFIG, "wp-config.php", config or dump),
        file_member(MemberRole.DUMP, "database.sql", dump),
    ]


def _write(
    tmp_path: Path,
    members: list[ArchiveMemberSource],
    builder: Builder,
    workspace: Path,
    cipher: object = None,
) -> tuple[Path, Manifest]:
    out_path = tmp_path / "archive.tar"
    with out_path.open("wb") as out:
        manifest = write_archive(
            out, builder, members, workspace=workspace, cipher=cipher
        )
    return out_path, manifest


@pytest.fixture
def written(
    tmp_path: Path,
    site_root: Path,
    dump_file: Path,
    manifest_builder: Builder,
    workspace: Path,
) -> tuple[Path, Manifest]:
    members = _members(site_root, dump_file, site_root / "wp-config.php")
    return _write(tmp_path, members, manifest_builder, workspace)


def test_the_outer_tar_is_uncompressed_and_holds_manifest_then_payload(
    written: tuple[Path, Manifest], archive_reader: Callable[..., Any]
) -> None:
    path, _ = written
    with path.open("rb") as handle:
        assert handle.read(2) != b"\x1f\x8b"  # not gzip
    read = archive_reader(path)
    assert read.outer == [MANIFEST_MEMBER, PAYLOAD_MEMBER]
    assert read.payload[:2] == b"\x1f\x8b"  # the payload is gzip
    assert gzip.decompress(read.payload)  # and decompresses


def test_the_payload_starts_with_the_dump_then_config_then_site_root(
    written: tuple[Path, Manifest], archive_reader: Callable[..., Any]
) -> None:
    read = archive_reader(written[0])
    roles = [info.name.split("/")[0] for info in read.inner]
    assert roles[0] == MemberRole.DUMP.value
    assert roles[1] == MemberRole.CONFIG.value
    assert set(roles[2:]) == {MemberRole.SITE_ROOT.value}


def test_the_manifest_records_the_stored_payload_size_and_sha256(
    written: tuple[Path, Manifest], archive_reader: Callable[..., Any]
) -> None:
    path, manifest = written
    read = archive_reader(path)
    assert read.manifest == manifest
    assert manifest.payload.name == PAYLOAD_MEMBER
    assert manifest.payload.size == len(read.payload)
    assert manifest.payload.sha256 == hashlib.sha256(read.payload).hexdigest()
    assert manifest.encrypted is False


def test_inner_members_and_index_entries_correspond_one_to_one(
    written: tuple[Path, Manifest], archive_reader: Callable[..., Any]
) -> None:
    read = archive_reader(written[0])
    inner = [(i.name, i.size if i.isfile() else 0) for i in read.inner]
    index = [(m.path, m.size) for m in read.manifest.members]
    assert inner == index
    assert len({name for name, _ in inner}) == len(inner)
    kinds = {m.path: m.kind for m in read.manifest.members}
    for info in read.inner:
        expected = {
            MemberKind.FILE: info.isfile(),
            MemberKind.DIR: info.isdir(),
            MemberKind.SYMLINK: info.issym(),
        }
        assert expected[kinds[info.name]], info.name


def test_an_empty_directory_is_a_dir_entry(
    written: tuple[Path, Manifest], archive_reader: Callable[..., Any]
) -> None:
    read = archive_reader(written[0])
    empty = member_path(MemberRole.SITE_ROOT, "wp-content/empty")
    assert [m.kind for m in read.manifest.members if m.path == empty] == [
        MemberKind.DIR
    ]
    assert [i.isdir() for i in read.inner if i.name == empty] == [True]


def test_a_relative_symlink_stores_its_target_as_the_link_name(
    tmp_path: Path,
    site_root: Path,
    dump_file: Path,
    manifest_builder: Builder,
    workspace: Path,
    archive_reader: Callable[..., Any],
) -> None:
    (site_root / "link").symlink_to("index.php")
    (site_root / "abs").symlink_to("/etc/hostname")
    with capture_logs() as logs:
        path, _ = _write(
            tmp_path, _members(site_root, dump_file), manifest_builder, workspace
        )
    read = archive_reader(path)
    by_name = {i.name: i for i in read.inner}
    index = {m.path: m for m in read.manifest.members}
    for name, target in (("link", "index.php"), ("abs", "/etc/hostname")):
        member = member_path(MemberRole.SITE_ROOT, name)
        assert by_name[member].issym()
        assert by_name[member].linkname == target == index[member].link_target
    warned = [e["path"] for e in logs if e["event"] == "symlink_outside_site_root"]
    assert warned == ["abs"]


def test_a_hard_linked_pair_is_two_files_and_no_hard_link_member(
    tmp_path: Path,
    site_root: Path,
    dump_file: Path,
    manifest_builder: Builder,
    workspace: Path,
    archive_reader: Callable[..., Any],
) -> None:
    os.link(site_root / "index.php", site_root / "twin.php")
    path, _ = _write(
        tmp_path, _members(site_root, dump_file), manifest_builder, workspace
    )
    read = archive_reader(path)
    assert not [i for i in read.inner if i.type == tarfile.LNKTYPE]
    first = read.contents[member_path(MemberRole.SITE_ROOT, "index.php")]
    second = read.contents[member_path(MemberRole.SITE_ROOT, "twin.php")]
    assert first == second == (site_root / "index.php").read_bytes()


def test_an_out_of_root_config_is_the_config_member_only(
    tmp_path: Path,
    dump_file: Path,
    manifest_builder: Builder,
    workspace: Path,
    archive_reader: Callable[..., Any],
) -> None:
    root = tmp_path / "public"
    root.mkdir()
    config = tmp_path / "wp-config.php"
    config.write_text("<?php // outside\n")

    def builder(index: tuple[ManifestMember, ...]) -> Manifest:
        return dataclasses.replace(
            manifest_builder(index), config_path=ConfigLocation.PARENT.value
        )

    path, _ = _write(tmp_path, _members(root, dump_file, config), builder, workspace)
    read = archive_reader(path)
    names = [i.name for i in read.inner]
    assert member_path(MemberRole.CONFIG, "wp-config.php") in names
    assert not [n for n in names if ".." in n.split("/")]
    assert read.manifest.config_path == "../wp-config.php"
    assert member_path(MemberRole.SITE_ROOT, "wp-config.php") not in names
    config_member = member_path(MemberRole.CONFIG, "wp-config.php")
    assert read.contents[config_member] == config.read_bytes()


def test_an_in_root_config_travels_twice_with_equal_content(
    written: tuple[Path, Manifest],
    site_root: Path,
    archive_reader: Callable[..., Any],
) -> None:
    read = archive_reader(written[0])
    config = read.contents[member_path(MemberRole.CONFIG, "wp-config.php")]
    in_tree = read.contents[member_path(MemberRole.SITE_ROOT, "wp-config.php")]
    assert config == in_tree == (site_root / "wp-config.php").read_bytes()


def test_a_file_changing_size_before_it_is_written_fails(
    tmp_path: Path,
    site_root: Path,
    dump_file: Path,
    manifest_builder: Builder,
    workspace: Path,
) -> None:
    members = _members(site_root, dump_file)
    (site_root / "index.php").write_text("<?php\n// grown after collection\n")
    with pytest.raises(ArchiveWriteError) as caught:
        _write(tmp_path, members, manifest_builder, workspace)
    assert member_path(MemberRole.SITE_ROOT, "index.php") in caught.value.reason
    assert list(workspace.iterdir()) == []


def test_a_file_growing_while_it_is_written_fails(
    tmp_path: Path,
    site_root: Path,
    dump_file: Path,
    manifest_builder: Builder,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The size matches when opened; bytes beyond it appear during the copy."""
    target = site_root / "wp-content" / "uploads" / "a.bin"
    members = _members(site_root, dump_file)
    original = tarfile.TarFile.addfile

    def growing(self: tarfile.TarFile, info: tarfile.TarInfo, *args: object) -> None:
        original(self, info, *args)
        if info.name.endswith("a.bin"):
            with target.open("ab") as handle:
                handle.write(b"late")

    monkeypatch.setattr(tarfile.TarFile, "addfile", growing)
    with pytest.raises(ArchiveWriteError) as caught:
        _write(tmp_path, members, manifest_builder, workspace)
    assert "a.bin" in caught.value.reason


def test_a_file_vanishing_fails_naming_it(
    tmp_path: Path,
    site_root: Path,
    dump_file: Path,
    manifest_builder: Builder,
    workspace: Path,
) -> None:
    members = _members(site_root, dump_file)
    (site_root / "index.php").unlink()
    with pytest.raises(ArchiveWriteError) as caught:
        _write(tmp_path, members, manifest_builder, workspace)
    assert "index.php" in caught.value.reason


# --------------------------------------------------------------------------
# Encrypted
# --------------------------------------------------------------------------


def test_the_encrypted_payload_reverses_to_the_plain_members(
    tmp_path: Path,
    site_root: Path,
    dump_file: Path,
    manifest_builder: Builder,
    workspace: Path,
    cipher: object,
    archive_reader: Callable[..., Any],
) -> None:
    members = _members(site_root, dump_file)
    (tmp_path / "plain").mkdir()
    plain_path, _ = _write(tmp_path / "plain", members, manifest_builder, workspace)
    with capture_logs() as logs:
        path, manifest = _write(tmp_path, members, manifest_builder, workspace, cipher)
    configured = [e for e in logs if e["event"] == "encryption_configured"]
    assert [(e["scheme"], e["mode"]) for e in configured] == [("age", "passphrase")]
    read = archive_reader(path, decrypt=cipher)
    plain = archive_reader(plain_path)

    assert read.outer == [MANIFEST_MEMBER, ENCRYPTED_PAYLOAD_MEMBER]
    assert manifest.encrypted is True
    assert manifest.payload.name == ENCRYPTED_PAYLOAD_MEMBER
    assert manifest.payload.size == len(read.payload)
    assert manifest.payload.sha256 == hashlib.sha256(read.payload).hexdigest()
    assert read.payload[:2] != b"\x1f\x8b"  # the stored bytes are ciphertext
    # ADR-0024 amendment: the inner Manifest is the encrypted payload's last
    # member, and the only difference from the plain payload.
    assert [i.name for i in read.inner] == [
        *(i.name for i in plain.inner),
        INNER_MANIFEST_MEMBER,
    ]
    sealed = dict(read.contents)
    inner_manifest = sealed.pop(INNER_MANIFEST_MEMBER)
    assert sealed == plain.contents
    assert check_inner_manifest(manifest_to_json(manifest), inner_manifest) is None
    assert cipher.encrypt_calls == 1
    assert list(workspace.iterdir()) == []
    verify_or_raise(path)


def test_the_encrypted_manifest_is_plaintext_and_free_of_key_material(
    tmp_path: Path,
    site_root: Path,
    dump_file: Path,
    manifest_builder: Builder,
    workspace: Path,
    cipher: object,
    archive_reader: Callable[..., Any],
) -> None:
    path, _ = _write(
        tmp_path, _members(site_root, dump_file), manifest_builder, workspace, cipher
    )
    text = archive_reader(path, decrypt=cipher).manifest_text
    document = json.loads(text)  # plaintext JSON, not ciphertext
    assert document["encrypted"] is True
    keys: set[str] = set()

    def walk(value: object) -> None:
        if isinstance(value, dict):
            keys.update(value)
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(document)
    forbidden = {"recipient", "recipients", "key", "salt", "kdf", "passphrase"}
    assert not keys & forbidden
    assert not [k for k in keys if "key" in k.lower() or "salt" in k.lower()]


def test_a_cipher_failing_mid_payload_leaves_the_workspace_empty(
    tmp_path: Path,
    site_root: Path,
    dump_file: Path,
    manifest_builder: Builder,
    workspace: Path,
    cipher_factory: type,
) -> None:
    (site_root / "big.bin").write_bytes(os.urandom(512 * 1024))
    failing = cipher_factory(fail_after=1)
    with pytest.raises(ArchiveWriteError):
        _write(
            tmp_path,
            _members(site_root, dump_file),
            manifest_builder,
            workspace,
            failing,
        )
    assert list(workspace.iterdir()) == []


# --------------------------------------------------------------------------
# The publish gate (verify_or_raise)
# --------------------------------------------------------------------------


def test_the_verify_or_raise_passes_a_written_archive(
    written: tuple[Path, Manifest],
) -> None:
    verify_or_raise(written[0])


def _tar(path: Path, members: list[tuple[str, bytes]]) -> Path:
    with tarfile.open(path, mode="w") as out:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            out.addfile(info, io.BytesIO(data))
    return path


def _manifest_bytes(written: tuple[Path, Manifest]) -> tuple[bytes, bytes]:
    with tarfile.open(written[0]) as archive:
        manifest = archive.extractfile(MANIFEST_MEMBER)
        payload = archive.extractfile(PAYLOAD_MEMBER)
        assert manifest is not None
        assert payload is not None
        return manifest.read(), payload.read()


def test_the_verify_or_raise_refuses_a_payload_first(
    tmp_path: Path, written: tuple[Path, Manifest]
) -> None:
    manifest, payload = _manifest_bytes(written)
    bad = _tar(
        tmp_path / "bad.tar", [(PAYLOAD_MEMBER, payload), (MANIFEST_MEMBER, manifest)]
    )
    with pytest.raises(VerificationFailedError) as caught:
        verify_or_raise(bad)
    assert caught.value.result.failures[0].reason is (
        VerificationReason.UNRECOGNISED_FORMAT
    )


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda m, p: [(MANIFEST_MEMBER, b"{not json")], id="bad-json"),
        pytest.param(lambda m, p: [(MANIFEST_MEMBER, m)], id="no-payload"),
        pytest.param(
            lambda m, p: [(MANIFEST_MEMBER, m), (PAYLOAD_MEMBER, p + b"x")],
            id="payload-size",
        ),
        pytest.param(
            lambda m, p: [(MANIFEST_MEMBER, m), (PAYLOAD_MEMBER, bytes(len(p)))],
            id="payload-digest",
        ),
        pytest.param(
            lambda m, p: [(MANIFEST_MEMBER, m), (ENCRYPTED_PAYLOAD_MEMBER, p)],
            id="payload-name",
        ),
        pytest.param(
            lambda m, p: [(MANIFEST_MEMBER, m), (PAYLOAD_MEMBER, p), ("extra", b"")],
            id="extra-member",
        ),
    ],
)
def test_the_verify_or_raise_refuses_a_malformed_archive(
    tmp_path: Path,
    written: tuple[Path, Manifest],
    mutate: Callable[[bytes, bytes], list[tuple[str, bytes]]],
) -> None:
    manifest, payload = _manifest_bytes(written)
    bad = _tar(tmp_path / "bad.tar", mutate(manifest, payload))
    with pytest.raises(VerificationFailedError):
        verify_or_raise(bad)


def test_the_verify_or_raise_refuses_a_non_tar(tmp_path: Path) -> None:
    junk = tmp_path / "junk"
    junk.write_bytes(b"\x00" * 10 + b"not a tar at all")
    with pytest.raises(VerificationFailedError):
        verify_or_raise(junk)


def test_a_directory_swapped_for_a_symlink_after_collection_fails(
    tmp_path: Path,
    site_root: Path,
    dump_file: Path,
    manifest_builder: Builder,
    workspace: Path,
) -> None:
    """A same-size file reached through a swapped parent is never written."""
    uploads = site_root / "wp-content" / "uploads"
    members = _members(site_root, dump_file)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.bin").write_bytes(b"\xff" * (uploads / "a.bin").stat().st_size)
    uploads.rename(tmp_path / "moved")
    uploads.symlink_to(outside)
    with pytest.raises(ArchiveWriteError) as caught:
        _write(tmp_path, members, manifest_builder, workspace)
    assert "uploads" in caught.value.reason
    assert list(workspace.iterdir()) == []


def test_a_file_replaced_by_another_of_the_same_size_fails(
    tmp_path: Path,
    site_root: Path,
    dump_file: Path,
    manifest_builder: Builder,
    workspace: Path,
) -> None:
    """Only the inode check can see this: same path, size and type."""
    target = site_root / "index.php"
    members = _members(site_root, dump_file)
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"\xff" * target.stat().st_size)
    replacement.replace(target)
    with pytest.raises(ArchiveWriteError) as caught:
        _write(tmp_path, members, manifest_builder, workspace)
    assert "index.php" in caught.value.reason


def test_a_file_swapped_for_a_fifo_after_collection_fails_without_hanging(
    tmp_path: Path,
    site_root: Path,
    dump_file: Path,
    manifest_builder: Builder,
    workspace: Path,
) -> None:
    """BL-E002-062: a FIFO with no writer must not block the open."""
    import threading

    target = site_root / "index.php"
    members = _members(site_root, dump_file)
    target.unlink()
    os.mkfifo(target)
    raised: list[BaseException] = []

    def write() -> None:
        try:
            _write(tmp_path, members, manifest_builder, workspace)
        except BaseException as exc:
            raised.append(exc)

    worker = threading.Thread(target=write, daemon=True)
    worker.start()
    worker.join(timeout=10)
    if worker.is_alive():
        # Release the blocked open so the daemon thread can finish.
        os.close(os.open(target, os.O_WRONLY | os.O_NONBLOCK))
        worker.join(timeout=10)
        pytest.fail("the write blocked opening a FIFO")
    assert len(raised) == 1
    assert isinstance(raised[0], ArchiveWriteError)
    assert "index.php" in raised[0].reason
