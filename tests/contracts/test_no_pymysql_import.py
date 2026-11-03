"""AD-7/ADR-0015 — this package imports no ``pymysql`` and builds no MySQL URL.

The database is reached only through ``l3io-wordpress-database``. A driver
import here would be a second connection path, and a ``mysql://`` or
``mysql+`` literal a connection string that could carry a credential — the
one form string redaction cannot catch once it is URL-encoded.

AST-based like ``test_no_mysql_client.py``: every module, walked.
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = PROJECT_ROOT / "src" / "l3io" / "wp" / "backup"
DRIVERS = ("pymysql", "MySQLdb", "mysql.connector")
URL_PREFIXES = ("mysql://", "mysql+", "mariadb://", "mariadb+")


def _imports(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
        return [node.module]
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name | ast.Attribute)
        and getattr(node.func, "id", getattr(node.func, "attr", "")) == "import_module"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    ):
        return [str(node.args[0].value)]
    return []


def _offenders(root: Path) -> list[str]:
    found: list[str] = []
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            where = f"{path.relative_to(root)}:{getattr(node, 'lineno', '?')}"
            found.extend(
                f"{where} imports {name}"
                for name in _imports(node)
                if any(name == d or name.startswith(f"{d}.") for d in DRIVERS)
            )
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value.lower().startswith(URL_PREFIXES)
            ):
                found.append(f"{where} builds a MySQL URL")
    return sorted(found)


def test_the_package_has_source_to_check() -> None:
    assert len(list(PACKAGE.rglob("*.py"))) > 10, f"no Python source under {PACKAGE}"


def test_no_module_imports_a_mysql_driver_or_builds_a_url() -> None:
    offenders = _offenders(PACKAGE)
    assert not offenders, f"reach MySQL through l3io-wp-database only: {offenders}"


def test_the_guard_fires_on_every_form_in_a_nested_module(tmp_path: Path) -> None:
    nested = tmp_path / "adapters" / "deep"
    nested.mkdir(parents=True)
    (nested / "a.py").write_text("import pymysql.cursors\n", encoding="utf-8")
    (nested / "b.py").write_text(
        "from pymysql import connect\n"
        "import importlib\nimportlib.import_module('pymysql')\n",
        encoding="utf-8",
    )
    (tmp_path / "c.py").write_text(
        "url = 'mysql+pymysql://u:p@h/db'\n", encoding="utf-8"
    )
    assert len(_offenders(tmp_path)) == 4
