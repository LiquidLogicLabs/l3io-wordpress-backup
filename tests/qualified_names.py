"""Resolve the names a module uses to where they were imported from.

Not a test module. An AST guard that matches a call by the spelling at the
call site misses ``from signal import signal``, ``import signal as s`` and a
relative import; one that matches the *qualified* name these helpers return
does not. ``package`` is the dotted package the scanned file sits in, so a
relative import resolves as Python would resolve it.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

__all__ = ["ImportTable", "module_package", "qualified"]


def module_package(path: Path, *, root: Path, root_package: str) -> str:
    """The package ``path`` (a file under ``root``) belongs to."""
    parts = path.relative_to(root).parent.parts
    return ".".join((root_package, *parts))


class ImportTable:
    """Local name -> fully qualified origin, from every import in ``tree``."""

    def __init__(self, tree: ast.AST, *, package: str) -> None:
        self.origins: dict[str, str] = {}
        self.imported: list[tuple[int, str]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.imported.append((node.lineno, alias.name))
                    if alias.asname:
                        self.origins[alias.asname] = alias.name
                    else:
                        head = alias.name.split(".")[0]
                        self.origins[head] = head
            elif isinstance(node, ast.ImportFrom):
                base = _absolute(node, package)
                for alias in node.names:
                    origin = f"{base}.{alias.name}" if base else alias.name
                    self.imported.append((node.lineno, origin))
                    self.origins[alias.asname or alias.name] = origin

    def resolve(self, node: ast.expr) -> str | None:
        """``node``'s qualified name, or its spelling when nothing imported it."""
        return qualified(node, self.origins)

    def references(self, tree: ast.AST) -> Iterator[tuple[int, str]]:
        """Every resolvable ``Name`` or ``Attribute`` in ``tree``, qualified."""
        for node in ast.walk(tree):
            if isinstance(node, ast.Name | ast.Attribute):
                name = self.resolve(node)
                if name is not None:
                    yield node.lineno, name

    def calls(self, tree: ast.AST) -> Iterator[tuple[int, str]]:
        """Every call in ``tree`` whose callee resolves, qualified."""
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = self.resolve(node.func)
                if name is not None:
                    yield node.lineno, name


def _absolute(node: ast.ImportFrom, package: str) -> str:
    if not node.level:
        return node.module or ""
    parts = package.split(".")
    base = parts[: len(parts) - (node.level - 1)]
    return ".".join([*base, node.module] if node.module else base)


def qualified(node: ast.expr, origins: dict[str, str]) -> str | None:
    """``a.b.c`` with ``a`` replaced by its origin; ``None`` for a non-name."""
    if isinstance(node, ast.Name):
        return origins.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        owner = qualified(node.value, origins)
        return None if owner is None else f"{owner}.{node.attr}"
    return None
