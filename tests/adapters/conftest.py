"""Shared fixtures for the Archive writer, traversal, store and executor tests."""

from __future__ import annotations

import io
import tarfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

import pytest

from l3io.wp.backup.domain.archive_format import (
    FORMAT_VERSION,
    PAYLOAD_MEMBER,
    Manifest,
    ManifestMember,
    ManifestPayload,
    manifest_from_json,
)

XOR_KEY = 0x5A
CHUNK = 64 * 1024


@dataclass
class XorCipher:
    """A test-only reversible ``PayloadCipher``: XOR with a constant, chunked."""

    encrypt_calls: int = 0
    fail_after: int | None = None
    seen: list[str] = field(default_factory=list)

    def check(self) -> None:
        self.seen.append("check")

    def _xor(self, reader: IO[bytes], writer: IO[bytes]) -> None:
        written = 0
        while chunk := reader.read(CHUNK):
            writer.write(bytes(b ^ XOR_KEY for b in chunk))
            written += len(chunk)
            if self.fail_after is not None and written >= self.fail_after:
                msg = "cipher failed mid-payload"
                raise OSError(msg)

    def encrypt(self, reader: IO[bytes], writer: IO[bytes]) -> None:
        self.encrypt_calls += 1
        self._xor(reader, writer)

    def decrypt(self, reader: IO[bytes], writer: IO[bytes]) -> None:
        self._xor(reader, writer)


@dataclass
class ReadArchive:
    """An Archive read back: outer names, the Manifest, inner members and bytes."""

    outer: list[str]
    outer_types: list[bytes]
    manifest: Manifest
    manifest_text: str
    payload: bytes
    inner: list[tarfile.TarInfo]
    contents: dict[str, bytes]


def read_archive(path: Path, *, decrypt: XorCipher | None = None) -> ReadArchive:
    """Read ``path`` back with stdlib ``tarfile``."""
    with tarfile.open(path, mode="r:") as outer:
        infos = outer.getmembers()
        manifest_file = outer.extractfile(infos[0])
        assert manifest_file is not None
        text = manifest_file.read().decode("utf-8")
        payload_file = outer.extractfile(infos[1])
        assert payload_file is not None
        payload = payload_file.read()
    plain = payload
    if decrypt is not None:
        out = io.BytesIO()
        decrypt.decrypt(io.BytesIO(payload), out)
        plain = out.getvalue()
    contents: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(plain), mode="r:gz") as inner:
        members = inner.getmembers()
        for member in members:
            if member.isfile():
                handle = inner.extractfile(member)
                assert handle is not None
                contents[member.name] = handle.read()
    return ReadArchive(
        outer=[info.name for info in infos],
        outer_types=[info.type for info in infos],
        manifest=manifest_from_json(text),
        manifest_text=text,
        payload=payload,
        inner=members,
        contents=contents,
    )


@pytest.fixture
def archive_reader() -> Callable[..., ReadArchive]:
    return read_archive


def simple_manifest(index: tuple[ManifestMember, ...]) -> Manifest:
    """A manifest builder for writer tests; the writer sets ``payload``."""
    return Manifest(
        format_version=FORMAT_VERSION,
        created_at="2026-11-02T00:00:00+00:00",
        backup_scope="all",
        source={"db_name": "wptest"},
        encrypted=False,
        payload=ManifestPayload(name=PAYLOAD_MEMBER, size=0, sha256="0" * 64),
        members=index,
        dump_present=True,
    )


@pytest.fixture
def manifest_builder() -> Callable[[tuple[ManifestMember, ...]], Manifest]:
    return simple_manifest


@pytest.fixture
def cipher() -> XorCipher:
    return XorCipher()


@pytest.fixture
def cipher_factory() -> type[XorCipher]:
    return XorCipher


@pytest.fixture
def site_root(tmp_path: Path) -> Path:
    """A small Site Root: config, files, a nested tree and an empty directory."""
    root = tmp_path / "site"
    (root / "wp-content" / "uploads").mkdir(parents=True)
    (root / "wp-content" / "empty").mkdir()
    (root / "wp-config.php").write_text("<?php\n// site config\n", encoding="utf-8")
    (root / "index.php").write_text("<?php\n// index\n", encoding="utf-8")
    (root / "wp-content" / "uploads" / "a.bin").write_bytes(b"\x00\x01" * 1000)
    return root


@pytest.fixture
def dump_file(tmp_path: Path) -> Path:
    path = tmp_path / "dumps" / "database.sql"
    path.parent.mkdir()
    path.write_text("CREATE TABLE t (id INT);\n", encoding="utf-8")
    return path


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "workspace"
    path.mkdir(mode=0o700)
    return path
