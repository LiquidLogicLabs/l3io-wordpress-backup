"""AD-28/ADR-0015 — this package issues no SQL DDL; database does.

Every string and bytes literal under ``src/l3io/wp/backup/`` — each f-string
also joined whole, its interpolations read as empty, docstrings excepted — is
searched, case-insensitively and across lines, for a DDL or privilege
statement's opening (``CREATE`` — with MariaDB's ``OR REPLACE`` and
``TEMPORARY`` — ``DROP``/``ALTER``/``TRUNCATE``/``RENAME`` of
a database, schema, table, user, role, index or view; ``GRANT``/``REVOKE``;
``SET PASSWORD``; ``FLUSH PRIVILEGES``). AD-28's grant escaping is satisfied by
database's own quoting; nothing here builds a statement (FR-9, E003-S02-004:
``ensure`` provisions).

**Not seen:** a statement assembled from separate literals by ``+``,
``join`` or ``%`` formatting, or read from a file at run time; review covers
those (BL-E002-089).

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
    r"\b(?:(?:CREATE(?:\s+OR\s+REPLACE)?(?:\s+TEMPORARY)?|DROP|ALTER|TRUNCATE"
    r"|RENAME)\s+"
    r"(?:DATABASE|SCHEMA|TABLE|USER|ROLE|INDEX|VIEW)"
    r"|GRANT\s+\S+.*\bON\b|REVOKE\s+\S+.*\bFROM\b"
    r"|SET\s+PASSWORD\b|FLUSH\s+PRIVILEGES\b)",
    re.IGNORECASE | re.DOTALL,
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


def _texts(tree: ast.Module) -> list[tuple[int, str]]:
    """``(line, text)`` for each literal, and each f-string joined whole."""
    exempt = _docstrings(tree)
    texts = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and id(node) not in exempt:
            if isinstance(node.value, str):
                texts.append((node.lineno, node.value))
            elif isinstance(node.value, bytes):
                texts.append((node.lineno, node.value.decode("latin-1")))
        elif isinstance(node, ast.JoinedStr):
            joined = "".join(
                str(part.value) if isinstance(part, ast.Constant) else ""
                for part in node.values
            )
            texts.append((node.lineno, joined))
    return texts


def ddl_literals(root: Path) -> list[str]:
    """Each literal under ``root`` that opens a DDL statement, once per line."""
    found = set()
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found.update(
            f"{path.relative_to(root)}:{line}"
            for line, text in _texts(tree)
            if DDL.search(text)
        )
    return sorted(found)


def test_the_walk_reaches_the_database_adapter() -> None:
    modules = list(PACKAGE.rglob("*.py"))
    assert len(modules) > 10, f"no Python source under {PACKAGE}"
    assert PACKAGE / "adapters" / "database.py" in modules
    assert PACKAGE / "adapters" / "database_load.py" in modules


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


def test_planted_account_statements_are_seen(tmp_path: Path) -> None:
    """FR-9 (E003-S02-004): the account half of ``ensure`` is database's too."""
    (tmp_path / "e.py").write_text(
        'a = "CREATE USER %s@%s IDENTIFIED BY %s"\n'
        'b = "alter user x"\n'
        'c = "DROP USER IF EXISTS x"\n'
        'd = "DROP DATABASE IF EXISTS x"\n',
        encoding="utf-8",
    )
    assert ddl_literals(tmp_path) == ["e.py:1", "e.py:2", "e.py:3", "e.py:4"]


def test_planted_ddl_the_widened_scan_sees(tmp_path: Path) -> None:
    """BL-E002-089: bytes, a split f-string, a multi-line grant, and the rest."""
    (tmp_path / "f.py").write_text(
        'a = b"DROP TABLE wp_posts"\n'
        'b = f"CREATE {kind} DATABASE {name}"\n'
        "c = f\"DROP {''}DATABASE x\"\n"
        'd = """GRANT ALL\n  PRIVILEGES\n  ON x.* TO u"""\n'
        'e = "SET PASSWORD FOR u = x"\n'
        'g = "flush privileges"\n'
        'h = "CREATE ROLE r"\n'
        'i = "CREATE OR REPLACE USER u"\n'
        'j = "create or replace database d"\n'
        'k = "CREATE TEMPORARY TABLE t (id int)"\n',
        encoding="utf-8",
    )
    lines = [1, 2, 3, 4, 7, 8, 9, 10, 11, 12]
    assert ddl_literals(tmp_path) == sorted(f"f.py:{n}" for n in lines)


def test_a_docstring_naming_ddl_is_not_a_statement(tmp_path: Path) -> None:
    (tmp_path / "d.py").write_text(
        '"""Database runs DROP DATABASE for us."""\n'
        "def f():\n"
        '    """And CREATE USER."""\n',
        encoding="utf-8",
    )
    assert ddl_literals(tmp_path) == []
