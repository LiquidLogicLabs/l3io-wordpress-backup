"""BL-E003-077 — a directory tree this package creates is owner-only throughout.

``Path.mkdir(parents=True)`` creates every missing ancestor with the umask's
mode, whatever ``mode=`` says, so only the leaf would be ``0700``. The one way
to create a tree is ``adapters/workspace.py::private_directories``. This module
refuses a ``mkdir`` call with ``parents=True`` anywhere in the package; the scope
is every ``.py`` file under the package root, found by walking it.

**Not covered:** a tree created by another call (``os.makedirs``) is refused by
the same scan; a directory created by a dependency is not seen.
"""

from __future__ import annotations

import ast
import os
import stat
from pathlib import Path

import l3io.wp.backup as package
from l3io.wp.backup.adapters.workspace import PRIVATE_DIR_MODE, private_directories

ROOT = Path(package.__file__).parent


def _modules() -> list[Path]:
    return sorted(ROOT.rglob("*.py"))


def _tree_creations(source: str) -> list[int]:
    """Lines of ``<x>.mkdir(..., parents=True)`` and ``os.makedirs(...)``."""
    lines: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        with_parents = any(
            k.arg == "parents"
            and not (isinstance(k.value, ast.Constant) and k.value.value is False)
            for k in node.keywords
        )
        if node.func.attr == "makedirs" or (node.func.attr == "mkdir" and with_parents):
            lines.append(node.lineno)
    return lines


def test_the_scan_covers_the_package() -> None:
    names = {p.relative_to(ROOT).as_posix() for p in _modules()}
    assert {"adapters/journal.py", "adapters/lock.py", "cli/restore.py"} <= names


def test_no_module_creates_a_tree_with_the_umask() -> None:
    found = {
        path.relative_to(ROOT).as_posix(): lines
        for path in _modules()
        if (lines := _tree_creations(path.read_text(encoding="utf-8")))
    }
    assert found == {}


def test_a_planted_tree_creation_is_caught() -> None:
    """The scope attack: each spelling the guard exists for is reported."""
    planted = (
        "path.mkdir(mode=0o700, parents=True, exist_ok=True)\n"
        "Path(x).mkdir(parents=flag)\n"
        "os.makedirs(x, mode=0o700)\n"
        "path.mkdir(mode=0o700)\n"
        "path.mkdir(parents=False)\n"
    )
    assert _tree_creations(planted) == [1, 2, 3]


def test_every_created_directory_is_owner_only(tmp_path: Path) -> None:
    previous = os.umask(0o022)
    try:
        private_directories(tmp_path / "a" / "b" / "c")
    finally:
        os.umask(previous)
    for directory in (tmp_path / "a", tmp_path / "a" / "b", tmp_path / "a" / "b" / "c"):
        assert stat.S_IMODE(directory.stat().st_mode) == PRIVATE_DIR_MODE, directory


def test_an_existing_directory_is_left_as_it_is(tmp_path: Path) -> None:
    existing = tmp_path / "shared"
    existing.mkdir()
    existing.chmod(0o755)
    private_directories(existing / "mine")
    assert stat.S_IMODE(existing.stat().st_mode) == 0o755
    assert stat.S_IMODE((existing / "mine").stat().st_mode) == PRIVATE_DIR_MODE
