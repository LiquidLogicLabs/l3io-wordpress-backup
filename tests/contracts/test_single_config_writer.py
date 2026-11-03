"""AD-18.4 — ``l3io.wp.config`` is imported only in ``adapters/config_writer.py``.

That module is the one writer (and the one reader) of ``wp-config.php``
through ``l3io-wordpress-config``; ``adapters/database.py`` reads
``WP_SITEURL``/``WP_HOME`` through its ``read_defined``. Database's own
``WpConfigFile`` (``l3io.wp.database``) is a different class and is not
covered here.

**Scope, derived.** Every ``*.py`` under ``src/l3io/wp/backup/``, walked
recursively, asserted non-empty, and asserted to see the writer's own import
(so the check is not vacuous). An import is any ``import l3io.wp.config[...]``,
``from l3io.wp.config[...] import ...`` or ``from l3io.wp import config``,
at any depth in a module, function-scope included; a relative import is
resolved against the module's own package first (``l3io.wp`` is a namespace
shared with ``l3io.wp.config``, so ``from ...config import x`` inside
``adapters/`` reaches it). The planted cases attack the scope: a nested
module, an alias, a submodule, a function-scope import and a relative one.
Not covered: a module named by a string at runtime
(``importlib.import_module("l3io.wp.config")``); review covers that.
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = PROJECT_ROOT / "src" / "l3io" / "wp" / "backup"
WRITER = Path("adapters") / "config_writer.py"
CONFIG_MODULE = "l3io.wp.config"
PACKAGE_NAME = "l3io.wp.backup"


def _absolute(node: ast.ImportFrom, package: list[str]) -> str | None:
    """The module an ``ImportFrom`` names, a relative one resolved."""
    if node.level == 0:
        return node.module
    if node.level > len(package):
        return None
    base = package[: len(package) - node.level + 1]
    return ".".join([*base, *([node.module] if node.module else [])])


def _imports_config(node: ast.AST, package: list[str]) -> bool:
    if isinstance(node, ast.Import):
        return any(
            alias.name == CONFIG_MODULE or alias.name.startswith(f"{CONFIG_MODULE}.")
            for alias in node.names
        )
    if isinstance(node, ast.ImportFrom):
        module = _absolute(node, package)
        if module is None:
            return False
        if module == CONFIG_MODULE or module.startswith(f"{CONFIG_MODULE}."):
            return True
        return module == "l3io.wp" and any(a.name == "config" for a in node.names)
    return False


def config_importers(root: Path, package: str = PACKAGE_NAME) -> list[str]:
    """``path:line`` of every import of ``l3io.wp.config`` under ``root``.

    ``root`` is the directory of the package named ``package``.
    """
    found = []
    for path in root.rglob("*.py"):
        parts = [*package.split("."), *path.relative_to(root).parent.parts]
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found.extend(
            f"{path.relative_to(root).as_posix()}:{node.lineno}"
            for node in ast.walk(tree)
            if _imports_config(node, parts)
        )
    return sorted(found)


def test_the_walk_sees_the_writer_import_it() -> None:
    assert len(list(PACKAGE.rglob("*.py"))) > 10, f"no source under {PACKAGE}"
    found = config_importers(PACKAGE)
    assert any(entry.startswith(f"{WRITER.as_posix()}:") for entry in found)


def test_only_the_config_writer_imports_l3io_wp_config() -> None:
    others = [
        entry
        for entry in config_importers(PACKAGE)
        if not entry.startswith(f"{WRITER.as_posix()}:")
    ]
    assert others == [], f"AD-18.4 — only {WRITER} may import {CONFIG_MODULE}"


def test_planted_imports_are_seen_however_spelled(tmp_path: Path) -> None:
    deep = tmp_path / "cli" / "deep"
    deep.mkdir(parents=True)
    (deep / "a.py").write_text("from l3io.wp import config as c\n", encoding="utf-8")
    (tmp_path / "b.py").write_text(
        "import l3io.wp.config.adapters.config_file as f\n", encoding="utf-8"
    )
    (tmp_path / "c.py").write_text(
        "def f():\n    from l3io.wp.config import WpConfigString\n",
        encoding="utf-8",
    )
    (tmp_path / "d.py").write_text(
        "from l3io.wp import database\nimport l3io.wp.configuration\n"
        "from . import config\nfrom .config import x\n",
        encoding="utf-8",
    )
    (deep / "e.py").write_text(
        "from ....config import WpConfigFile\n", encoding="utf-8"
    )
    assert config_importers(tmp_path) == [
        "b.py:1",
        "c.py:2",
        "cli/deep/a.py:1",
        "cli/deep/e.py:1",
    ]
