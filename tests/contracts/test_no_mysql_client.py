"""ADR-0015 — credentials reach MySQL client tools only through l3io-wp-database.

This package never invokes ``mysqldump``, ``mysql``, ``mariadb-dump`` or
``mariadb`` itself: database's ``dump``/``load`` do, with credentials passed
through a pipe option file. A second transport here would be a second
implementation of AD-7's guarantee, tested against nothing. FR-9
(E003-S02-004): the restore's provisioning and load are database's too.

The check is on string literals naming a client binary — the bare name (an
``argv[0]``), the name followed by arguments, or an absolute path ending in
it — anywhere in the package source, docstrings included. It cannot see a
name assembled at runtime; review covers that.

**Scope, derived.** Every ``*.py`` under the package, walked recursively,
asserted non-empty and to reach ``adapters/database.py`` and
``adapters/database_load.py`` (the modules that drive database's load). The planted
cases attack the scope — a nested module, a path, an ``argv`` list.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = PROJECT_ROOT / "src" / "l3io" / "wp" / "backup"

CLIENTS = ("mysqldump", "mysqlpump", "mysqladmin", "mariadb-dump", "mariadb", "mysql")
_CLIENT = re.compile(
    r"^(?:\S*/)?(?:" + "|".join(re.escape(c) for c in CLIENTS) + r")(?:\s.*)?$",
    re.DOTALL,
)


def client_literals(root: Path) -> list[str]:
    """``<relative path>:<line>: <literal>`` for each literal naming a client."""
    found = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found.extend(
            f"{path.relative_to(root)}:{node.lineno}: {node.value!r}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and _CLIENT.match(node.value)
        )
    return found


def test_the_walk_reaches_the_database_modules() -> None:
    """Scope assertion. Without it the test below passes over an empty set."""
    modules = set(PACKAGE.rglob("*.py"))
    assert len(modules) > 10, f"no Python source under {PACKAGE}"
    assert PACKAGE / "adapters" / "database.py" in modules
    assert PACKAGE / "adapters" / "database_load.py" in modules


def test_no_module_names_a_mysql_client_binary() -> None:
    offenders = client_literals(PACKAGE)
    assert not offenders, (
        "call l3io-wp-database's dump/load instead of a client binary "
        f"(ADR-0015): {offenders}"
    )


def test_planted_client_invocations_are_seen_wherever_they_are(
    tmp_path: Path,
) -> None:
    deep = tmp_path / "adapters" / "deep"
    deep.mkdir(parents=True)
    (deep / "a.py").write_text(
        'import subprocess\nsubprocess.run(["mariadb", "-e", "x"])\n',
        encoding="utf-8",
    )
    (tmp_path / "b.py").write_text(
        'TOOL = "/usr/local/bin/mysqldump"\nCMD = "mysql --defaults-file=x"\n',
        encoding="utf-8",
    )
    assert [line.split(": ")[0] for line in client_literals(tmp_path)] == [
        "adapters/deep/a.py:2",
        "b.py:1",
        "b.py:2",
    ]


def test_prose_naming_a_client_is_not_an_invocation(tmp_path: Path) -> None:
    (tmp_path / "c.py").write_text(
        'TEXT = "database runs mysql for us"\nNAME = "mysql_native_password"\n',
        encoding="utf-8",
    )
    assert client_literals(tmp_path) == []
