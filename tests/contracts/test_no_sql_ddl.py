"""AD-28/ADR-0015 — this package issues no SQL DDL; database does.

Every string literal under ``src/l3io/wp/backup/`` — f-string parts included,
docstrings excepted — is searched, case-insensitively, for a DDL or privilege
statement's opening (``CREATE``/``DROP``/``ALTER``/``TRUNCATE``/``RENAME`` of
a database, schema, table, user, index or view; ``GRANT``/``REVOKE``). AD-28's
grant escaping is satisfied by database's own quoting; nothing here builds a
statement.

**Scope, derived.** Every ``*.py`` under the package, walked recursively and
asserted non-empty and to reach ``adapters/database.py`` (the module that
calls database's DDL-issuing functions). The planted cases attack the scope —
a nested module, lowercase SQL, an f-string — and show a docstring is exempt.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = PROJECT_ROOT / "src" / "l3io" / "wp" / "backup"

DDL = re.compile(
    r"\b(?:(?:CREATE|DROP|ALTER|TRUNCATE|RENAME)\s+"
    r"(?:DATABASE|SCHEMA|TABLE|USER|INDEX|VIEW)"
    r"|GRANT\s+\S+.*\bON\b|REVOKE\s+\S+.*\bFROM\b)",
    re.IGNORECASE,
)


def _docstrings(tree: ast.Module) -> set[int]:
    owners = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, owners) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                found.add(id(first.value))
    return found


def ddl_literals(root: Path) -> list[str]:
    """Each string literal under ``root`` that opens a DDL statement."""
    found = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        exempt = _docstrings(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in exempt
                and DDL.search(node.value)
            ):
                found.append(f"{path.relative_to(root)}:{node.lineno}")
    return found


def test_the_walk_reaches_the_database_adapter() -> None:
    modules = list(PACKAGE.rglob("*.py"))
    assert len(modules) > 10, f"no Python source under {PACKAGE}"
    assert PACKAGE / "adapters" / "database.py" in modules


def test_no_string_literal_in_the_package_is_ddl() -> None:
    assert ddl_literals(PACKAGE) == []


def test_planted_ddl_is_seen_wherever_and_however_it_is_spelled(
    tmp_path: Path,
) -> None:
    deep = tmp_path / "adapters" / "deep"
    deep.mkdir(parents=True)
    (deep / "a.py").write_text('q = "drop   table wp_posts"\n', encoding="utf-8")
    (tmp_path / "b.py").write_text(
        'q = f"GRANT ALL PRIVILEGES ON {name}.* TO %s"\n', encoding="utf-8"
    )
    (tmp_path / "c.py").write_text(
        'q = "REVOKE ALL PRIVILEGES ON x.* FROM u"\nr = "Create Database wp"\n',
        encoding="utf-8",
    )
    assert ddl_literals(tmp_path) == [
        "adapters/deep/a.py:1",
        "b.py:1",
        "c.py:1",
        "c.py:2",
    ]


def test_a_docstring_naming_ddl_is_not_a_statement(tmp_path: Path) -> None:
    (tmp_path / "d.py").write_text(
        '"""Database runs DROP DATABASE for us."""\n'
        "def f():\n"
        '    """And CREATE USER."""\n',
        encoding="utf-8",
    )
    assert ddl_literals(tmp_path) == []
