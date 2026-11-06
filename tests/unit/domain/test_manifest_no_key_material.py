"""FR-4, ADR-0014/ADR-0024 — no key material in a Manifest (E004-S01-003).

Run against Manifests the encrypted path wrote with the real backend: the
outer ``manifest.json`` and the inner Manifest the payload ends with (ADR-0024
amendment). No key anywhere in either JSON object tree matches
:data:`KEY_MATERIAL`, and the passphrase occurs in neither text. ``age``
carries its own header; the Manifest records only ``encrypted: true`` and the
payload's size and sha256.

Also the committed encrypted fixture (ADR-0014, "including an encrypted
one"): it authenticates with its sidecar passphrase, and the generator
reproduces its Manifest's structure (``age``'s random file key and salt, and
``created_at``, make the bytes differ).
"""

from __future__ import annotations

import importlib.util
import json
import re
import tarfile
import uuid
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any

import pytest

from l3io.wp.backup.adapters.crypto import PassphraseCipher
from l3io.wp.backup.adapters.tar import (
    TarArchiveProbe,
    decrypt_payload,
    read_inner_manifest,
)
from l3io.wp.backup.cli.verify import VerifyRequest, drive_verify
from l3io.wp.backup.domain.archive_format import (
    MANIFEST_MEMBER,
    classify,
    manifest_from_json,
)
from l3io.wp.backup.domain.storage_target import StorageTarget
from tests.conftest import seal_archive

KEY_MATERIAL = re.compile(
    r"key|kdf|salt|passphrase|identity|recipient|scrypt|nonce", re.IGNORECASE
)
PROJECT_ROOT = Path(__file__).resolve().parents[3]
V1 = PROJECT_ROOT / "tests" / "fixtures" / "archives" / "v1"
FIXTURE = V1 / "encrypted-passphrase.tar.gz"


def _keys(value: object) -> list[str]:
    """Every object key in a JSON tree, however deep."""
    if isinstance(value, dict):
        return [k for k, v in value.items() for k in (k, *_keys(v))]
    if isinstance(value, list):
        return [k for item in value for k in _keys(item)]
    return []


def _outer_text(archive: Path) -> str:
    with tarfile.open(archive) as outer:
        handle = outer.extractfile(MANIFEST_MEMBER)
        assert handle is not None
        return handle.read().decode("utf-8")


def _target(archive: Path) -> StorageTarget:
    return StorageTarget(scheme="file", path=PurePosixPath(archive))


def _inner_text(archive: Path, passphrase: str, workdir: Path) -> str:
    target = _target(archive)
    view = classify(TarArchiveProbe().member_index(target))
    plain = decrypt_payload(
        target, view, cipher=PassphraseCipher(passphrase), destination=workdir / "p"
    )
    return read_inner_manifest(plain).decode("utf-8")


@pytest.fixture(scope="module")
def sealed(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str, str, str]:
    passphrase = f"no-key-pp-{uuid.uuid4().hex}"
    workdir = tmp_path_factory.mktemp("sealed")
    archive = seal_archive(workdir, passphrase)
    return (
        archive,
        passphrase,
        _outer_text(archive),
        _inner_text(archive, passphrase, workdir),
    )


def test_the_keys_walk_reaches_nested_keys() -> None:
    """Scope: a key buried in a list of objects is found."""
    assert "salt" in _keys({"a": [{"b": {"salt": 1}}]})


@pytest.mark.parametrize("which", ["outer", "inner"])
def test_no_manifest_key_names_key_material(
    sealed: tuple[Path, str, str, str], which: str
) -> None:
    _, passphrase, outer, inner = sealed
    text = outer if which == "outer" else inner
    document = json.loads(text)
    assert document["encrypted"] is True
    offending = [k for k in _keys(document) if KEY_MATERIAL.search(k)]
    assert not offending, f"key material in the {which} Manifest: {offending}"
    assert passphrase not in text


def test_a_planted_key_material_field_is_caught() -> None:
    document = {"payload": {"name": "x", "kdf": {"scrypt_n": 18}}}
    assert [k for k in _keys(document) if KEY_MATERIAL.search(k)] == [
        "kdf",
        "scrypt_n",
    ]


# --------------------------------------------------------------------------
# The committed encrypted fixture
# --------------------------------------------------------------------------


def _generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("v1_generate", V1 / "generate.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_committed_fixture_authenticates_with_its_sidecar() -> None:
    passphrase = _generator().read_passphrase()
    request = VerifyRequest(archive=_target(FIXTURE), authenticate=True)
    assert drive_verify(request, ctx=None, passphrase=passphrase).passed


def _structure(text: str) -> dict[str, Any]:
    manifest = manifest_from_json(text)
    document: dict[str, Any] = json.loads(text)
    assert manifest.encrypted is True
    del document["created_at"], document["payload"]["size"]
    del document["payload"]["sha256"]
    return document


def test_the_generator_reproduces_the_fixtures_manifest(tmp_path: Path) -> None:
    generator = _generator()
    regenerated = tmp_path / "encrypted-passphrase.tar.gz"
    generator.write(regenerated, passphrase=generator.read_passphrase())
    assert _structure(_outer_text(regenerated)) == _structure(_outer_text(FIXTURE))
