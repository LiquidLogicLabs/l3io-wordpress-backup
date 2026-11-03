"""AD-14 — every exception the package defines derives from ``BackupError``.

Scope is derived by walking ``src/l3io/wp/backup/`` for ``*.py`` files — not
``pkgutil``, which would silently skip a directory added without an
``__init__.py`` — and importing each. Each walker is also run over a planted
tree that hides a violation where a narrower scope would miss it.

Also pinned here (ADR-0018): the three refusals are ``RefusedError``s, and no
``domain/`` module imports ``adapters/`` or ``cli/``. import-linter already
enforces the second; this states the reason next to the hierarchy.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import sys
from pathlib import Path

import pytest

from l3io.wp.backup.domain.errors import (
    ArchiveExistsError,
    BackupError,
    DatabaseRefusedError,
    RefusedError,
    UnsupportedStorageSchemeError,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC = PROJECT_ROOT / "src"
PACKAGE = SRC / "l3io" / "wp" / "backup"


def _module_names(root: Path, package_dir: Path) -> list[str]:
    """Every module under ``package_dir``, as a dotted name relative to ``root``."""
    names = []
    for path in sorted(package_dir.rglob("*.py")):
        parts = path.relative_to(root).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        names.append(".".join(parts))
    return names


def _exception_classes(modules: list[str]) -> list[type[BaseException]]:
    """Every ``Exception`` subclass *defined* (not imported) in ``modules``."""
    found: list[type[BaseException]] = []
    for name in modules:
        module = importlib.import_module(name)
        found.extend(
            obj
            for _, obj in inspect.getmembers(module, inspect.isclass)
            if issubclass(obj, Exception) and obj.__module__ == name
        )
    return found


def _strays(classes: list[type[BaseException]]) -> list[str]:
    return sorted(
        f"{c.__module__}.{c.__qualname__}"
        for c in classes
        if not issubclass(c, BackupError)
    )


def test_the_walk_finds_the_package_and_its_errors() -> None:
    """Scope assertion: an empty or misdirected walk would pass vacuously."""
    modules = _module_names(SRC, PACKAGE)
    assert "l3io.wp.backup.domain.errors" in modules
    assert "l3io.wp.backup.adapters.database" in modules
    assert BackupError in _exception_classes(modules)


def test_every_exception_defined_in_the_package_is_a_backup_error() -> None:
    strays = _strays(_exception_classes(_module_names(SRC, PACKAGE)))
    assert not strays, f"AD-14 — not a BackupError subclass: {strays}"


def test_the_walk_reaches_a_module_beside_no_init_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scope attack: a stray hidden two levels down, in a directory with no
    ``__init__.py`` — invisible to ``pkgutil.walk_packages``."""
    package = tmp_path / "plantedpkg"
    (package / "deep" / "deeper").mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "deep" / "deeper" / "rogue.py").write_text(
        "from l3io.wp.backup.domain.errors import BackupError\n"
        "class Fine(BackupError): ...\n"
        "class Rogue(Exception): ...\n"
        "class AlsoRogue(ValueError): ...\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        strays = _strays(_exception_classes(_module_names(tmp_path, package)))
    finally:
        for name in [m for m in sys.modules if m.startswith("plantedpkg")]:
            del sys.modules[name]
    assert strays == [
        "plantedpkg.deep.deeper.rogue.AlsoRogue",
        "plantedpkg.deep.deeper.rogue.Rogue",
    ]


@pytest.mark.parametrize(
    "refusal", [UnsupportedStorageSchemeError, ArchiveExistsError, DatabaseRefusedError]
)
def test_the_refusals_are_refused_errors(refusal: type[BackupError]) -> None:
    assert issubclass(refusal, RefusedError)


def _outward_imports(files: list[Path]) -> list[str]:
    outward = ("l3io.wp.backup.adapters", "l3io.wp.backup.cli")
    found = []
    for path in files:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module] + [
                    f"{node.module}.{alias.name}" for alias in node.names
                ]
            else:
                continue
            found.extend(
                f"{path.name}: {name}" for name in names if name.startswith(outward)
            )
    return found


def test_no_domain_module_imports_adapters_or_cli() -> None:
    files = sorted((PACKAGE / "domain").rglob("*.py"))
    assert any(p.name == "errors.py" for p in files), "domain/ scope is empty"
    assert not _outward_imports(files)


def test_the_domain_import_check_sees_a_nested_member_import(tmp_path: Path) -> None:
    """Scope attack: a nested file, and the layer imported as a *member*."""
    nested = tmp_path / "domain" / "sub"
    nested.mkdir(parents=True)
    (nested / "leak.py").write_text(
        "from l3io.wp.backup import cli\n", encoding="utf-8"
    )
    files = sorted((tmp_path / "domain").rglob("*.py"))
    assert _outward_imports(files) == ["leak.py: l3io.wp.backup.cli"]
