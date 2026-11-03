"""AD-21/AG-13 — no port that moves Archive or Dump bytes takes or returns ``bytes``.

Every Protocol under ``ports/`` is walked with :func:`typing.get_type_hints`:
each method's parameters and return, each property getter's, and the class's
own data-attribute annotations (``stream: bytes`` has no entry in ``vars()``).
An annotation fails when ``bytes`` (or ``bytearray`` or ``memoryview``) appears
anywhere in it — alone, in a union, or as an argument of any generic
(``list[bytes]``, ``dict[str, bytes]``, ``Sequence[tuple[str, bytes]]``,
``Callable[[bytes], None]``), since each of those buffers bytes whole.

The one exception is the file object, ``IO[bytes]`` / ``BinaryIO``: the
streaming interface AD-21 names. Chunked iteration (``Iterator[bytes]``) is
**not** allowed: no port needs it, and a port that wants it must widen
``STREAMING_ORIGINS`` here, visibly, in review.

Scope is the ``ports/`` directory walked for ``*.py`` files, asserted
non-empty, and the walker is run over a planted tree that hides violations in
a nested directory with no ``__init__.py``, behind a property, in a class-level
annotation and inside container generics.
"""

from __future__ import annotations

import importlib
import inspect
import sys
import typing
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC = PROJECT_ROOT / "src"
PORTS = SRC / "l3io" / "wp" / "backup" / "ports"

BYTE_TYPES = (bytes, bytearray, memoryview)
#: Generic origins whose ``bytes`` argument is a stream, not a buffer.
STREAMING_ORIGINS = (typing.IO,)


def _module_names(root: Path, package_dir: Path) -> list[str]:
    names = []
    for path in sorted(package_dir.rglob("*.py")):
        parts = path.relative_to(root).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        names.append(".".join(parts))
    return names


def _protocols(modules: list[str]) -> list[type]:
    found = []
    for name in modules:
        module = importlib.import_module(name)
        found.extend(
            obj
            for _, obj in inspect.getmembers(module, inspect.isclass)
            if obj.__module__ == name and getattr(obj, "_is_protocol", False)
        )
    return found


def _is_bytes(annotation: object) -> bool:
    if isinstance(annotation, list):  # Callable's parameter list
        return any(_is_bytes(arg) for arg in annotation)
    if annotation in BYTE_TYPES:
        return True
    if typing.get_origin(annotation) in STREAMING_ORIGINS:
        return False
    return any(_is_bytes(arg) for arg in typing.get_args(annotation))


def _violations(protocol: type) -> list[str]:
    found = [
        f"{protocol.__name__}.{attr}: attribute"
        for attr, hint in typing.get_type_hints(protocol).items()
        if _is_bytes(hint)
    ]
    for attr, member in vars(protocol).items():
        function = member.fget if isinstance(member, property) else member
        if not inspect.isfunction(function):
            continue
        hints = typing.get_type_hints(function)
        found.extend(
            f"{protocol.__name__}.{attr}: {param}"
            for param, hint in hints.items()
            if _is_bytes(hint)
        )
    return found


def test_the_ports_scope_is_not_empty() -> None:
    protocols = {p.__name__ for p in _protocols(_module_names(SRC, PORTS))}
    assert {"DatabaseCommand", "StagedArchive", "PayloadCipher"} <= protocols


def test_no_port_signature_is_bytes() -> None:
    violations = [
        v for p in _protocols(_module_names(SRC, PORTS)) for v in _violations(p)
    ]
    assert not violations, f"AD-21 — a port moves bytes as bytes: {violations}"


def test_the_walk_finds_bytes_hidden_where_a_narrow_scope_would_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "plantedports"
    (package / "nested").mkdir(parents=True)
    (package / "nested" / "leaky.py").write_text(
        "from __future__ import annotations\n"
        "from collections.abc import Callable, Sequence\n"
        "from typing import IO, BinaryIO, Protocol\n"
        "class Leaky(Protocol):\n"
        "    data: bytes\n"
        "    def all(self) -> list[bytes]: ...\n"
        "    def pairs(self) -> Sequence[tuple[str, bytes]]: ...\n"
        "    def each(self, sink: Callable[[bytes], None]) -> None: ...\n"
        "    def read_all(self) -> bytes | None: ...\n"
        "    def write(self, data: bytearray, *, sink: IO[bytes]) -> None: ...\n"
        "    @property\n"
        "    def body(self) -> memoryview: ...\n"
        "class Fine(Protocol):\n"
        "    stream: IO[bytes]\n"
        "    def copy(self, reader: IO[bytes], raw: BinaryIO) -> list[str]: ...\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        protocols = _protocols(_module_names(tmp_path, package))
        violations = sorted(v for p in protocols for v in _violations(p))
    finally:
        for name in [m for m in sys.modules if m.startswith("plantedports")]:
            del sys.modules[name]
    assert {p.__name__ for p in protocols} == {"Leaky", "Fine"}
    assert violations == [
        "Leaky.all: return",
        "Leaky.body: return",
        "Leaky.data: attribute",
        "Leaky.each: sink",
        "Leaky.pairs: return",
        "Leaky.read_all: return",
        "Leaky.write: data",
    ]
