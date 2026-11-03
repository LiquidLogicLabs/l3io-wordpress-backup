"""AD-4 — which ports are commands, and which ``AdapterSet`` fields hold one.

Shared by ``tests/contracts/test_port_split.py``,
``tests/unit/cli/test_adapter_sets.py`` and
``tests/unit/cli/test_preflight_order.py`` so the three decide "is this field
a command?" the same way.

**Scope, derived.** :func:`protocols` walks every ``*.py`` under a package
directory and imports it; :func:`command_protocols` is every ``*Command``
Protocol so found under ``ports/`` (asserted non-empty). :func:`command_fields`
resolves a dataclass's annotations (``typing.get_type_hints``) and treats a
field as a command when **any** type inside its annotation — a union member,
an ``Optional``, a generic's argument, at any depth — is one of those
Protocols. A field's annotation text is never consulted, so
``ArchiveWriterCommand | None`` is a command and a class merely *named*
``...Command`` that is not a discovered Protocol is not.
"""

from __future__ import annotations

import dataclasses
import importlib
import inspect
import typing
from collections.abc import Collection, Iterator
from pathlib import Path
from types import ModuleType

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
PORTS = SRC / "l3io" / "wp" / "backup" / "ports"

__all__ = [
    "PORTS",
    "SRC",
    "command_fields",
    "command_protocols",
    "protocols",
]


def _modules(root: Path, package_dir: Path) -> list[ModuleType]:
    modules = []
    for path in sorted(package_dir.rglob("*.py")):
        parts = path.relative_to(root).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        modules.append(importlib.import_module(".".join(parts)))
    return modules


def protocols(root: Path, package_dir: Path) -> dict[str, type]:
    """Every ``Protocol`` class defined in a module under ``package_dir``."""
    found: dict[str, type] = {}
    for module in _modules(root, package_dir):
        for name, value in vars(module).items():
            if (
                inspect.isclass(value)
                and value.__module__ == module.__name__
                and getattr(value, "_is_protocol", False)
            ):
                found[name] = value
    return found


def command_protocols() -> frozenset[type]:
    """Every ``*Command`` Protocol under ``ports/``; never empty."""
    found = protocols(SRC, PORTS)
    commands = frozenset(p for n, p in found.items() if n.endswith("Command"))
    assert commands, f"no *Command Protocol found under {PORTS}"
    return commands


def _constituents(hint: object) -> Iterator[object]:
    """``hint`` and every type argument inside it, at any depth."""
    yield hint
    for argument in typing.get_args(hint):
        yield from _constituents(argument)


def command_fields(
    container: type, commands: Collection[type] | None = None
) -> list[str]:
    """``container``'s fields whose resolved type holds a command Protocol."""
    wanted = command_protocols() if commands is None else commands
    hints = typing.get_type_hints(container)
    return [
        f.name
        for f in dataclasses.fields(container)
        if any(
            isinstance(member, type) and member in wanted
            for member in _constituents(hints[f.name])
        )
    ]
