"""ADR-0015 amendment (BL-E003-111): ``cli/`` imports nothing from database at run time.

``cli/`` passes backup-owned ``CredentialSpec`` values; only ``adapters/``
builds or imports database objects. import-linter cannot forbid a subpackage
of the external ``l3io`` namespace, so this walks every module under ``cli/``
(derived from the tree, not listed). An import inside ``if TYPE_CHECKING:``
is never executed and is allowed (an annotation only); every other import of
``l3io.wp.database`` — module level, in a function, ``from l3io.wp import
database`` — is an offence. **Not covered:** ``importlib.import_module`` with a
computed name.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2] / "src" / "l3io" / "wp" / "backup"
CLI = PACKAGE / "cli"
DATABASE = "l3io.wp.database"


def _type_checking_nodes(tree: ast.Module) -> set[int]:
    """``id()`` of every node under an ``if TYPE_CHECKING:`` block."""
    inside: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and (
            (isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING")
            or (
                isinstance(node.test, ast.Attribute)
                and node.test.attr == "TYPE_CHECKING"
            )
        ):
            for child in node.body:
                inside.update(id(n) for n in ast.walk(child))
    return inside


def _names_database(node: ast.ImportFrom) -> bool:
    return any(alias.name == "database" for alias in node.names)


def _offences(source: str) -> list[int]:
    tree = ast.parse(source)
    skipped = _type_checking_nodes(tree)
    found = []
    for node in ast.walk(tree):
        if id(node) in skipped:
            continue
        if (
            isinstance(node, ast.Import)
            and any(
                a.name == DATABASE or a.name.startswith(DATABASE + ".")
                for a in node.names
            )
        ) or (
            isinstance(node, ast.ImportFrom)
            and (
                (node.module or "") == DATABASE
                or (node.module or "").startswith(DATABASE + ".")
                or (
                    node.module == "l3io.wp"
                    and any(a.name == "database" for a in node.names)
                )
            )
        ):
            found.append(node.lineno)
    return found


def test_the_scope_is_every_cli_module() -> None:
    assert len(list(CLI.rglob("*.py"))) > 5


def test_cli_imports_no_database_module_at_run_time() -> None:
    offenders = {
        str(path.relative_to(PACKAGE)): lines
        for path in CLI.rglob("*.py")
        if (lines := _offences(path.read_text(encoding="utf-8")))
    }
    assert offenders == {}


def test_each_spelling_is_found_and_type_checking_is_not() -> None:
    planted = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    from l3io.wp.database import CredentialSource\n"
        "import l3io.wp.database\n"
        "from l3io.wp import database as wpdb\n"
        "def f():\n    from l3io.wp.database.errors import X\n"
    )
    assert _offences(planted) == [4, 5, 7]
