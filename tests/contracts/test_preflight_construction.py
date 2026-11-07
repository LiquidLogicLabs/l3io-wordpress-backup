"""AD-13 (E008-S01-001, M1) -- the preflights are built in one module, and used.

An AST scan of every ``*.py`` under ``src/l3io/wp/backup/`` (``rglob``, with a
scope assertion) finds each call of ``DumpReadinessPreflight(``,
``BinaryPreflight(`` and ``RootPreflight(`` -- bare or through an attribute
chain -- and requires every one to be inside ``cli/preflight.py``, the module
whose factories (``root_preflight``, ``binary_preflight``) every root calls.

It also fails while Epic 5's ``BinaryPreflight``, ``diagnose`` or
``unsupported_platform`` has no caller under the package outside
``adapters/preflight.py``, and while either Run root (``cli/backup.py``,
``cli/restore.py``) stops calling ``root_preflight``: a planted removal of that
binding is reported.

Not covered: a preflight reached under another name (an alias import,
``getattr``), or a root that calls the factory and then discards its result.
"""

from __future__ import annotations

import ast
import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = PROJECT_ROOT / "src" / "l3io" / "wp" / "backup"
CLASSES = ("DumpReadinessPreflight", "BinaryPreflight", "RootPreflight")
OWNER = "cli/preflight.py"
#: Epic 5's names that must have a production caller (E005 closure, AD-1).
USED = ("BinaryPreflight", "diagnose", "unsupported_platform")
DEFINER = "adapters/preflight.py"
#: The roots whose preflight is the factory's (M1).
ROOTS = ("cli/backup.py", "cli/restore.py")
FACTORY = "root_preflight"


def _called(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _calls(root: Path) -> list[tuple[str, str]]:
    """``(file, called name)`` for every call in every module under ``root``."""
    found: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        relative = path.relative_to(root).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = _called(node)
                if name is not None:
                    found.append((relative, name))
    return found


def _misplaced(root: Path) -> list[tuple[str, str]]:
    return [
        (file, name) for file, name in _calls(root) if name in CLASSES and file != OWNER
    ]


def _uncalled(root: Path) -> list[str]:
    callers = {name for file, name in _calls(root) if file != DEFINER}
    return [name for name in USED if name not in callers]


def _unbound_roots(root: Path) -> list[str]:
    binding = {file for file, name in _calls(root) if name == FACTORY}
    return [file for file in ROOTS if file not in binding]


def test_the_scan_reaches_every_layer() -> None:
    files = {p.relative_to(PACKAGE).as_posix() for p in PACKAGE.rglob("*.py")}
    for expected in (OWNER, DEFINER, *ROOTS, "cli/doctor.py", "api/operations.py"):
        assert expected in files, f"scope missing {expected}"
    assert {name for _, name in _calls(PACKAGE)} >= set(CLASSES)


def test_preflights_are_built_only_in_cli_preflight() -> None:
    assert _misplaced(PACKAGE) == [], (
        f"AD-13 -- build preflights only through {OWNER}'s factories"
    )


def test_epic_5_preflight_has_production_callers() -> None:
    assert _uncalled(PACKAGE) == []


def test_both_run_roots_bind_the_factory() -> None:
    assert _unbound_roots(PACKAGE) == []


def test_a_construction_planted_outside_the_owner_is_found(tmp_path: Path) -> None:
    deep = tmp_path / "api" / "x"
    deep.mkdir(parents=True)
    (deep / "leak.py").write_text(
        "import p\n\nX = p.BinaryPreflight(cwd=1)\nY = DumpReadinessPreflight(1)\n",
        encoding="utf-8",
    )
    assert sorted(_misplaced(tmp_path)) == [
        ("api/x/leak.py", "BinaryPreflight"),
        ("api/x/leak.py", "DumpReadinessPreflight"),
    ]


def test_a_removed_root_binding_is_reported(tmp_path: Path) -> None:
    copy = tmp_path / "backup"
    shutil.copytree(PACKAGE, copy, ignore=shutil.ignore_patterns("__pycache__"))
    restore = copy / "cli" / "restore.py"
    text = restore.read_text(encoding="utf-8")
    assert f"{FACTORY}(" in text
    restore.write_text(text.replace(f"{FACTORY}(", "_unbound("), encoding="utf-8")
    assert _unbound_roots(copy) == ["cli/restore.py"]


def test_an_epic_5_name_left_uncalled_is_reported(tmp_path: Path) -> None:
    (tmp_path / "adapters").mkdir()
    (tmp_path / DEFINER).write_text(
        "def diagnose(): ...\nBinaryPreflight(cwd=1)\nunsupported_platform()\n",
        encoding="utf-8",
    )
    (tmp_path / "doctor.py").write_text("diagnose()\n", encoding="utf-8")
    assert _uncalled(tmp_path) == ["BinaryPreflight", "unsupported_platform"]
