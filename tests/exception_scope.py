"""The scope the AD-14 exception contracts share (E006-S01-002).

Everything here is derived from the tree, never listed by hand:

- :func:`module_names` walks ``*.py`` files on disk (not ``pkgutil``, which
  skips a directory without an ``__init__.py``), as
  ``tests/contracts/test_exception_hierarchy.py`` does;
- :func:`error_classes` follows ``BackupError.__subclasses__()`` recursively
  once those modules are imported;
- :func:`construction_sites` is an ``ast`` walk: a class is reached when its
  name follows ``raise`` or is called anywhere outside its own class body.

The contract tests run each function over a planted tree as well, so a
narrowed walk fails rather than passes.
"""

from __future__ import annotations

import ast
import importlib
from collections.abc import Iterable, Iterator
from pathlib import Path

from l3io.wp.backup.domain.errors import BackupError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
PACKAGE_DIR = SRC / "l3io" / "wp" / "backup"
PACKAGE = "l3io.wp.backup"


def module_names(root: Path, package_dir: Path) -> list[str]:
    """Every module under ``package_dir``, as a dotted name relative to ``root``."""
    names = []
    for path in sorted(package_dir.rglob("*.py")):
        parts = path.relative_to(root).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        names.append(".".join(parts))
    return names


def import_all(modules: Iterable[str]) -> None:
    for name in modules:
        importlib.import_module(name)


def _subclasses(cls: type[BaseException]) -> Iterator[type[BaseException]]:
    for sub in cls.__subclasses__():
        yield sub
        yield from _subclasses(sub)


def error_classes(prefix: str = PACKAGE) -> set[type[BaseException]]:
    """``BackupError`` and every subclass whose module is under ``prefix``.

    Call after :func:`import_all`: a class exists only once its module ran.
    """
    found = {BackupError, *_subclasses(BackupError)}
    return {
        c
        for c in found
        if c.__module__ == prefix or c.__module__.startswith(prefix + ".")
    }


def is_private(cls: type[BaseException]) -> bool:
    return cls.__qualname__.startswith("_") or ".<locals>." in cls.__qualname__


def exported_exceptions(names: Iterable[str], namespace: object) -> dict[str, type]:
    """The exception classes among ``names`` resolved on ``namespace``."""
    found: dict[str, type] = {}
    for name in names:
        obj = getattr(namespace, name)
        if isinstance(obj, type) and issubclass(obj, BaseException):
            found[name] = obj
    return found


def _called_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


class _Sites(ast.NodeVisitor):
    def __init__(self, where: str) -> None:
        self.where = where
        self.classes: list[str] = []
        self.found: dict[str, list[str]] = {}

    def _add(self, name: str | None, line: int) -> None:
        if name is None or name in self.classes:
            return  # a class constructing itself in its own body reaches nothing
        self.found.setdefault(name, []).append(f"{self.where}:{line}")

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.classes.append(node.name)
        self.generic_visit(node)
        self.classes.pop()

    def visit_Raise(self, node: ast.Raise) -> None:
        # ``raise X(...)`` is counted once, by visit_Call; ``raise X`` here.
        if node.exc is not None and not isinstance(node.exc, ast.Call):
            self._add(_called_name(node.exc), node.lineno)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        self._add(_called_name(node), node.lineno)
        self.generic_visit(node)


def construction_sites(files: Iterable[Path], root: Path) -> dict[str, list[str]]:
    """Each name raised or called in ``files``, with where (``path:line``)."""
    found: dict[str, list[str]] = {}
    for path in files:
        visitor = _Sites(str(path.relative_to(root)))
        visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
        for name, sites in visitor.found.items():
            found.setdefault(name, []).extend(sites)
    return found
