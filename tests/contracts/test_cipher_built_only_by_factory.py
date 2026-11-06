"""AD-4/AD-26 -- a cipher is built only through ``build_payload_cipher``.

An AST scan of every ``*.py`` under ``src/l3io/wp/backup/`` (``rglob``, with a
scope assertion) finds each call of ``PassphraseCipher(`` -- bare or through
any attribute chain (``crypto.PassphraseCipher(...)``) -- and requires that the
only one is inside ``adapters/crypto.py``'s function ``build_payload_cipher``.

Not covered: the class reached under another name (``cls = PassphraseCipher;
cls(...)``, an alias import, ``getattr``), or built by ``type(...)`` / a
subclass's constructor; code outside ``src/l3io/wp/backup/``.
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = PROJECT_ROOT / "src" / "l3io" / "wp" / "backup"
CLASS = "PassphraseCipher"
FACTORY = ("adapters/crypto.py", "build_payload_cipher")


def _called(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _constructions(root: Path) -> list[tuple[str, str | None]]:
    """``(file, enclosing top-level function)`` for each ``CLASS(`` call."""
    found: list[tuple[str, str | None]] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        relative = path.relative_to(root).as_posix()
        owners: dict[int, str | None] = {}
        for top in tree.body:
            owner = top.name if isinstance(top, ast.FunctionDef) else None
            for node in ast.walk(top):
                owners[id(node)] = owner
        calls = sorted(
            (n for n in ast.walk(tree) if isinstance(n, ast.Call)),
            key=lambda n: n.lineno,
        )
        found += [
            (relative, owners.get(id(node))) for node in calls if _called(node) == CLASS
        ]
    return found


def test_the_scan_reaches_the_factory_and_the_class() -> None:
    files = sorted(PACKAGE.rglob("*.py"))
    assert PACKAGE / "adapters" / "crypto.py" in files, "scope missing crypto.py"
    assert PACKAGE / "cli" / "restore.py" in files, "scope missing cli/"
    source = (PACKAGE / FACTORY[0]).read_text(encoding="utf-8")
    assert f"class {CLASS}" in source


def test_the_factory_is_the_only_constructor() -> None:
    assert _constructions(PACKAGE) == [FACTORY], (
        f"AD-26 -- build {CLASS} only through build_payload_cipher: "
        f"{_constructions(PACKAGE)}"
    )


def test_a_construction_planted_at_depth_is_found(tmp_path: Path) -> None:
    deep = tmp_path / "api" / "x"
    deep.mkdir(parents=True)
    (deep / "leak.py").write_text(
        "from m import PassphraseCipher\n"
        "import crypto\n\n"
        "def helper():\n    return PassphraseCipher('s')\n\n"
        "VALUE = crypto.PassphraseCipher('s')\n",
        encoding="utf-8",
    )
    (tmp_path / "fine.py").write_text(
        "def build_payload_cipher():\n    return Other('s')\n", encoding="utf-8"
    )
    assert _constructions(tmp_path) == [
        ("api/x/leak.py", "helper"),
        ("api/x/leak.py", None),
    ]
