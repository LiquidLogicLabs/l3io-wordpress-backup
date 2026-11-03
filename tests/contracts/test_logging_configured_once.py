"""AD-10 — logging is configured at one composition root, never in a library module.

No module under ``src/l3io/wp/backup/`` but ``cli/logging.py`` calls
``structlog.configure``, ``structlog.configure_once``, ``logging.basicConfig``,
anything in ``logging.config``, or adds a handler to the root logger.

Scope: every ``*.py`` under the package, walked and asserted non-empty. A call
is matched by its dotted name as written (``structlog.configure(...)``,
``logging.config.dictConfig(...)``), by a ``from`` import of a forbidden name,
and by **any** ``.addHandler(...)`` call — stricter than "the root logger", so a
root logger reached through a local name (``root = logging.getLogger()``) is
seen too; a library module has no reason to add a handler to any logger. An
aliased import (``import logging as lg``, ``import structlog as sl``) is
resolved per module before matching. A name bound at runtime
(``f = getattr(structlog, "configure")``) is not seen; review covers that.
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = PROJECT_ROOT / "src" / "l3io" / "wp" / "backup"
OWNER = PACKAGE / "cli" / "logging.py"

FORBIDDEN_CALLS = frozenset(
    {"structlog.configure", "structlog.configure_once", "logging.basicConfig"}
)
FORBIDDEN_IMPORTS = {
    "structlog": {"configure", "configure_once"},
    "logging": {"basicConfig", "config"},
}


def _aliases(tree: ast.AST) -> dict[str, str]:
    """``asname`` -> module for every ``import x as y`` in a module."""
    return {
        alias.asname: alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.asname is not None
    }


def _dotted(node: ast.expr, aliases: dict[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        inner = _dotted(node.value, aliases)
        return None if inner is None else f"{inner}.{node.attr}"
    return None


def _offence(node: ast.AST, aliases: dict[str, str] | None = None) -> str | None:
    if isinstance(node, ast.Call):
        name = _dotted(node.func, aliases or {})
        if name in FORBIDDEN_CALLS:
            return f"calls {name}"
        if name is not None and name.startswith("logging.config."):
            return f"calls {name}"
        if isinstance(node.func, ast.Attribute) and node.func.attr == "addHandler":
            return "adds a handler"
    if isinstance(node, ast.Import) and any(
        alias.name == "logging.config" for alias in node.names
    ):
        return "imports logging.config"
    if isinstance(node, ast.ImportFrom):
        names = {alias.name for alias in node.names}
        if node.module == "logging.config":
            return "imports from logging.config"
        forbidden = FORBIDDEN_IMPORTS.get(node.module or "", set()) & names
        if forbidden:
            return f"imports {sorted(forbidden)} from {node.module}"
    return None


def _offenders(root: Path, owner: Path) -> list[str]:
    found: list[str] = []
    for path in root.rglob("*.py"):
        if path == owner:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases = _aliases(tree)
        found.extend(
            f"{path.relative_to(root)}:{node.lineno} {offence}"
            for node in ast.walk(tree)
            if (offence := _offence(node, aliases)) is not None
        )
    return sorted(found)


def test_the_scope_is_the_whole_package() -> None:
    modules = list(PACKAGE.rglob("*.py"))
    assert len(modules) > 10, f"no Python source under {PACKAGE}"
    assert OWNER in modules
    layers = {p.relative_to(PACKAGE).parts[0] for p in modules if p.parent != PACKAGE}
    assert {"domain", "core", "ports", "adapters", "cli", "api"} <= layers


def test_the_owner_really_configures() -> None:
    """Otherwise excluding it would hide nothing, and the rule nothing."""
    found = [
        o
        for node in ast.walk(ast.parse(OWNER.read_text(encoding="utf-8")))
        if (o := _offence(node)) is not None
    ]
    assert "calls structlog.configure" in found
    assert "adds a handler" in found


def test_no_other_module_configures_logging() -> None:
    offenders = _offenders(PACKAGE, OWNER)
    assert not offenders, f"configure logging in cli/logging.py only: {offenders}"


def test_the_guard_fires_on_every_form_deep_in_the_tree(tmp_path: Path) -> None:
    (tmp_path / "cli").mkdir()
    (tmp_path / "adapters" / "deep").mkdir(parents=True)
    owner = tmp_path / "cli" / "logging.py"
    owner.write_text("structlog.configure()\n", encoding="utf-8")
    (tmp_path / "adapters" / "deep" / "x.py").write_text(
        "import logging.config\n"
        "structlog.configure(processors=[])\n"
        "logging.basicConfig()\n"
        "logging.config.dictConfig({})\n"
        "logging.getLogger().addHandler(h)\n"
        "root = logging.getLogger()\n"
        "root.addHandler(h)\n",
        encoding="utf-8",
    )
    (tmp_path / "adapters" / "y.py").write_text(
        "from structlog import configure_once\nfrom logging import basicConfig\n"
        "import logging as lg\nimport structlog as sl\n"
        "lg.basicConfig()\nsl.configure()\n",
        encoding="utf-8",
    )
    assert _offenders(tmp_path, owner) == [
        "adapters/deep/x.py:1 imports logging.config",
        "adapters/deep/x.py:2 calls structlog.configure",
        "adapters/deep/x.py:3 calls logging.basicConfig",
        "adapters/deep/x.py:4 calls logging.config.dictConfig",
        "adapters/deep/x.py:5 adds a handler",
        "adapters/deep/x.py:7 adds a handler",
        "adapters/y.py:1 imports ['configure_once'] from structlog",
        "adapters/y.py:2 imports ['basicConfig'] from logging",
        "adapters/y.py:5 calls logging.basicConfig",
        "adapters/y.py:6 calls structlog.configure",
    ]
