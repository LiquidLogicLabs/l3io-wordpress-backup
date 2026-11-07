"""ADR-0014/ADR-0024, AD-14, AD-21, AD-26 — ``adapters/crypto.py`` (E004-S01-003).

Runs over database's real passphrase backend (the ``encryption`` extra;
BL-E004-042 retired the ``FakeAge`` stand-in). scrypt makes each encrypt and
decrypt cost about two seconds, so one ciphertext is shared by the module.

Covered: round trips; the two decrypt failures, kept distinct and both exit
8; ``check()``'s refusals by backend; ``repr``; no ``bytes`` in a public
signature; the factory; no direct construction under ``cli/``; the backend's
tool requirements; and that the package holds exactly one ``PayloadCipher``.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import inspect
import io
import os
import typing
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import IO, get_type_hints

import pytest

pytest.importorskip("l3io.wp.database")

from l3io.wp.backup.adapters import crypto
from l3io.wp.backup.adapters.crypto import (
    EncryptionConfigError,
    PassphraseCipher,
    build_payload_cipher,
    passphrase_backend_requirements,
)
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.domain.errors import (
    MissingExtraError,
    PassphraseRejectedError,
    PayloadDamagedError,
)
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.domain.preflight import ToolRequirement
from l3io.wp.backup.ports.crypto import PayloadCipher

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = PROJECT_ROOT / "src" / "l3io" / "wp" / "backup"
BACKEND_ENV = "L3IO_AGE_PASSPHRASE_BACKEND"
#: More than one 64 KiB ``age`` chunk, so the stream has several.
MULTI_CHUNK = os.urandom(3 * 64 * 1024 + 17)


@pytest.fixture(scope="module")
def passphrase() -> str:
    return f"crypto-pp-{uuid.uuid4().hex}"


@pytest.fixture(scope="module")
def sealed(passphrase: str) -> bytes:
    out = io.BytesIO()
    PassphraseCipher(passphrase).encrypt(io.BytesIO(MULTI_CHUNK), out)
    return out.getvalue()


@pytest.fixture(autouse=True)
def _default_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(BACKEND_ENV, raising=False)


def _decrypt(cipher: PassphraseCipher, data: bytes) -> bytes:
    out = io.BytesIO()
    cipher.decrypt(io.BytesIO(data), out)
    return out.getvalue()


# --------------------------------------------------------------------------
# Round trips and the two decrypt failures
# --------------------------------------------------------------------------


def test_a_multi_chunk_stream_round_trips(sealed: bytes, passphrase: str) -> None:
    assert sealed.startswith(b"age-encryption.org/v1\n")
    assert MULTI_CHUNK not in sealed
    assert _decrypt(PassphraseCipher(passphrase), sealed) == MULTI_CHUNK


@pytest.mark.parametrize("plain", [b"", b"x"], ids=["empty", "one-byte"])
def test_a_short_stream_round_trips(plain: bytes, passphrase: str) -> None:
    cipher = PassphraseCipher(passphrase)
    out = io.BytesIO()
    cipher.encrypt(io.BytesIO(plain), out)
    assert _decrypt(cipher, out.getvalue()) == plain


def test_a_wrong_passphrase_is_rejected_not_damaged(
    sealed: bytes, passphrase: str
) -> None:
    from l3io.wp.database.errors import CorruptDumpError, DecryptionError

    with pytest.raises(PassphraseRejectedError) as caught:
        _decrypt(PassphraseCipher(f"wrong-{passphrase}"), sealed)
    assert caught.value.exit_code is ExitCode.VERIFICATION_FAILED
    assert isinstance(caught.value.__cause__, DecryptionError)
    assert not isinstance(caught.value, (DecryptionError, CorruptDumpError))
    assert passphrase not in str(caught.value)


def _flipped(data: bytes) -> bytes:
    flipped = bytearray(data)
    flipped[-100] ^= 0x01
    return bytes(flipped)


@pytest.mark.parametrize(
    "damage",
    [
        lambda d: d[:10],  # inside the header
        lambda d: d[: len(d) // 2],  # mid-stream
        lambda d: d[:-1],  # the last byte
        _flipped,
        lambda _: b"not an age file at all\n",
    ],
    ids=["header", "middle", "last-byte", "flipped", "not-age"],
)
def test_a_damaged_stream_is_damaged_not_rejected(
    sealed: bytes, passphrase: str, damage: object
) -> None:
    from l3io.wp.database.errors import CorruptDumpError, DecryptionError

    with pytest.raises(PayloadDamagedError) as caught:
        _decrypt(PassphraseCipher(passphrase), damage(sealed))  # type: ignore[operator]
    assert caught.value.exit_code is ExitCode.VERIFICATION_FAILED
    assert caught.value.field is None
    assert not isinstance(caught.value, (DecryptionError, CorruptDumpError))


# --------------------------------------------------------------------------
# check(): the refusals, by backend
# --------------------------------------------------------------------------


def test_check_passes_on_the_installed_backend(passphrase: str) -> None:
    PassphraseCipher(passphrase).check()


def test_an_empty_passphrase_is_a_config_error() -> None:
    with pytest.raises(EncryptionConfigError) as caught:
        PassphraseCipher("").check()
    assert caught.value.exit_code is ExitCode.USAGE


def test_an_unrecognised_backend_is_a_config_error(
    monkeypatch: pytest.MonkeyPatch, passphrase: str
) -> None:
    monkeypatch.setenv(BACKEND_ENV, "no-such-backend")
    with pytest.raises(EncryptionConfigError):
        PassphraseCipher(passphrase).check()


def test_the_cli_backend_without_age_on_path_is_a_config_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, passphrase: str
) -> None:
    monkeypatch.setenv(BACKEND_ENV, "cli")
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(EncryptionConfigError) as caught:
        PassphraseCipher(passphrase).check()
    assert passphrase not in str(caught.value)


@pytest.mark.parametrize("relative", ["", ".", "bin"], ids=["empty", "dot", "named"])
@pytest.mark.parametrize("backend", ["cli", "auto"])
def test_the_cli_backend_with_a_relative_path_entry_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    passphrase: str,
    backend: str,
    relative: str,
) -> None:
    """AR-3/RT-7: database resolves ``age`` through PATH, relative entries included."""
    monkeypatch.setenv(BACKEND_ENV, backend)
    if backend == "auto":  # auto resolves to cli only without the binder
        monkeypatch.setattr(importlib.util, "find_spec", lambda *_a, **_k: None)
    monkeypatch.setenv("PATH", os.pathsep.join([str(tmp_path), relative]))
    with pytest.raises(EncryptionConfigError) as caught:
        PassphraseCipher(passphrase).check()
    assert "relative" in str(caught.value)
    assert passphrase not in str(caught.value)


def test_a_relative_path_entry_does_not_matter_to_the_binder_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, passphrase: str
) -> None:
    monkeypatch.setenv(BACKEND_ENV, "binder")
    monkeypatch.setenv("PATH", os.pathsep.join([str(tmp_path), ""]))
    PassphraseCipher(passphrase).check()


def _hide_the_binder(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real = builtins.__import__

    def without(name: str, *args: object, **kwargs: object) -> object:
        if name.split(".")[0] == "l3io_age_passphrase":
            raise ImportError(name)
        return real(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", without)
    monkeypatch.setattr("l3io.wp.database._passphrase.binder_available", lambda: False)


def test_the_binder_backend_without_the_binder_is_a_missing_extra(
    monkeypatch: pytest.MonkeyPatch, passphrase: str
) -> None:
    monkeypatch.setenv(BACKEND_ENV, "binder")
    _hide_the_binder(monkeypatch)
    with pytest.raises(MissingExtraError) as caught:
        PassphraseCipher(passphrase).check()
    assert caught.value.extra == "database"
    assert caught.value.exit_code is ExitCode.NOT_READY


def test_no_usable_backend_under_auto_is_a_missing_extra(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, passphrase: str
) -> None:
    """F-18: auto (unset) with no binder and no ``age`` is ``MissingExtraError``."""
    _hide_the_binder(monkeypatch)
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(MissingExtraError):
        PassphraseCipher(passphrase).check()


# --------------------------------------------------------------------------
# repr, and no bytes in a public signature (AD-21)
# --------------------------------------------------------------------------


def test_repr_holds_no_passphrase(passphrase: str) -> None:
    cipher = PassphraseCipher(passphrase)
    assert passphrase not in repr(cipher)
    assert passphrase not in str(cipher)
    assert "redacted" in repr(cipher)


def _public_callables() -> Iterator[tuple[str, object]]:
    for name in crypto.__all__:
        value = getattr(crypto, name)
        if inspect.isclass(value):
            for attr, member in vars(value).items():
                if callable(member) and not attr.startswith("_"):
                    yield f"{name}.{attr}", member
        elif callable(value):
            yield name, value


_WHOLE_BUFFERS = (bytes, bytearray, memoryview)


def _buffers(hint: object) -> bool:
    """Whether ``hint`` names a whole buffer anywhere but as ``IO``'s argument."""
    if hint in _WHOLE_BUFFERS:
        return True
    if typing.get_origin(hint) is typing.IO:
        return False
    return any(_buffers(arg) for arg in typing.get_args(hint))


def test_the_buffer_walk_reaches_unions_and_spares_io() -> None:
    def planted(a: IO[bytes]) -> bytes | None: ...

    hints = get_type_hints(planted)
    assert _buffers(hints["return"])
    assert not _buffers(hints["a"])
    assert _buffers(typing.Optional[bytearray])  # noqa: UP045 - the old spelling


def test_no_public_signature_takes_or_returns_bytes() -> None:
    """Scope: every public callable reached from ``__all__``."""
    seen = dict(_public_callables())
    assert {"PassphraseCipher.encrypt", "PassphraseCipher.decrypt"} <= seen.keys()
    assert "build_payload_cipher" in seen
    for name, function in seen.items():
        hints = get_type_hints(function, vars(crypto))
        for hint in hints.values():
            assert not _buffers(hint), name


# --------------------------------------------------------------------------
# build_payload_cipher
# --------------------------------------------------------------------------


class SpyRegistry(SecretRegistry):
    def __init__(self) -> None:
        super().__init__()
        self.registered: list[str] = []

    def register(self, value: str, *, source: str = "unspecified") -> None:
        self.registered.append(value)
        super().register(value, source=source)


def test_the_factory_returns_none_for_no_passphrase() -> None:
    registry = SpyRegistry()
    assert build_payload_cipher(passphrase=None, secrets=registry) is None
    assert registry.registered == []


def test_the_factory_registers_then_checks_before_returning(
    monkeypatch: pytest.MonkeyPatch, passphrase: str
) -> None:
    registry = SpyRegistry()
    order: list[str] = []
    real_check = PassphraseCipher.check

    def check(self: PassphraseCipher) -> None:
        order.append("registered" if registry.registered else "unregistered")
        real_check(self)

    monkeypatch.setattr(PassphraseCipher, "check", check)
    cipher = build_payload_cipher(passphrase=passphrase, secrets=registry)
    assert isinstance(cipher, PassphraseCipher)
    assert [getattr(v, "reveal", lambda v=v: v)() for v in registry.registered] == [
        passphrase
    ]
    assert order == ["registered"]
    assert passphrase not in registry.redact(f"x {passphrase} y")


def test_the_factory_refuses_as_check_refuses() -> None:
    with pytest.raises(EncryptionConfigError):
        build_payload_cipher(passphrase="", secrets=SecretRegistry())


def _direct_constructions(root: Path) -> list[str]:
    found = []
    for path in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else getattr(func, "id", None)
            )
            if name == "PassphraseCipher":
                found.append(f"{path.name}:{node.lineno}")
    return found


def test_no_cli_module_constructs_the_cipher_directly() -> None:
    """F-2: every root under ``cli/`` goes through ``build_payload_cipher``."""
    cli = PACKAGE / "cli"
    assert (cli / "restore.py").is_file()
    assert (cli / "verify.py").is_file()
    assert _direct_constructions(cli) == []


def test_the_construction_scan_finds_a_planted_call(tmp_path: Path) -> None:
    nested = tmp_path / "deeper"
    nested.mkdir()
    (nested / "root.py").write_text(
        "from x import crypto\nc = crypto.PassphraseCipher('p')\n", encoding="utf-8"
    )
    (tmp_path / "other.py").write_text("c = PassphraseCipher('p')\n", encoding="utf-8")
    assert sorted(_direct_constructions(tmp_path)) == ["other.py:1", "root.py:2"]


# --------------------------------------------------------------------------
# passphrase_backend_requirements (Gate E004 AG-8)
# --------------------------------------------------------------------------

CLI_TOOLS = ("age", "age-plugin-batchpass")


@pytest.mark.parametrize(
    ("value", "binder", "expected"),
    [
        ("binder", True, ()),
        ("binder", False, ()),
        ("cli", True, CLI_TOOLS),
        ("cli", False, CLI_TOOLS),
        ("auto", True, ()),
        ("auto", False, CLI_TOOLS),
        (None, True, ()),
        (None, False, CLI_TOOLS),
        ("  CLI ", True, CLI_TOOLS),
        ("no-such-backend", False, ()),
    ],
)
def test_backend_requirements(
    monkeypatch: pytest.MonkeyPatch,
    value: str | None,
    binder: bool,
    expected: tuple[str, ...],
) -> None:
    real = importlib.util.find_spec

    def find_spec(name: str, *args: object) -> object:
        if name == "l3io_age_passphrase":
            return real(name) if binder else None
        return real(name, *args)  # type: ignore[arg-type]

    monkeypatch.setattr(importlib.util, "find_spec", find_spec)
    environ = {} if value is None else {BACKEND_ENV: value}
    found = passphrase_backend_requirements(environ=environ, needed_for="ENCRYPT")
    assert tuple(t.name for t in found) == expected
    assert all(t.needed_for == "ENCRYPT" for t in found)
    if expected:
        assert found[0] == ToolRequirement("age", (1, 3), "ENCRYPT")


def test_the_cipher_reports_the_process_backend(
    monkeypatch: pytest.MonkeyPatch, passphrase: str
) -> None:
    monkeypatch.setenv(BACKEND_ENV, "cli")
    tools = PassphraseCipher(passphrase).required_tools()
    assert tuple(t.name for t in tools) == CLI_TOOLS
    monkeypatch.setenv(BACKEND_ENV, "binder")
    assert PassphraseCipher(passphrase).required_tools() == ()


# --------------------------------------------------------------------------
# Exactly one PayloadCipher (BL-E004-042)
# --------------------------------------------------------------------------

_PORT_METHODS = tuple(name for name in vars(PayloadCipher) if not name.startswith("_"))


def _implementers(root: Path, package: str) -> set[str]:
    """Classes defined under ``root`` that structurally satisfy ``PayloadCipher``."""
    found = set()
    for path in sorted(root.rglob("*.py")):
        parts = path.relative_to(root).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        name = ".".join((package, *parts))
        module = importlib.import_module(name)
        for cls_name, cls in inspect.getmembers(module, inspect.isclass):
            if cls.__module__ != name or cls is PayloadCipher:
                continue
            if getattr(cls, "_is_protocol", False):
                continue
            if all(callable(getattr(cls, m, None)) for m in _PORT_METHODS):
                found.add(f"{name}.{cls_name}")
    return found


def test_the_package_holds_exactly_one_payload_cipher() -> None:
    assert set(_PORT_METHODS) == {"check", "encrypt", "decrypt"}
    found = _implementers(PACKAGE, "l3io.wp.backup")
    assert found == {"l3io.wp.backup.adapters.crypto.PassphraseCipher"}


def test_the_walk_finds_a_second_cipher_in_a_new_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "plantpkg"
    (package / "deep").mkdir(parents=True)
    (package / "deep" / "second.py").write_text(
        "class Rot13:\n"
        "    def check(self): ...\n"
        "    def encrypt(self, reader, writer): ...\n"
        "    def decrypt(self, reader, writer): ...\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    assert _implementers(package, "plantpkg") == {"plantpkg.deep.second.Rot13"}
