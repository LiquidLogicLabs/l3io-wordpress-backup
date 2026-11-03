"""AD-7/ADR-0015 — this package never sets ``MYSQL_PWD``.

A password in the environment is readable through ``/proc/<pid>/environ``.
Database's ``dump``/``load`` pass credentials through a pipe option file; this
package puts none in ``os.environ`` or an ``env=`` mapping, so the name
``MYSQL_PWD`` has no reason to appear in its source at all.

AST-based like ``test_no_mysql_client.py``: every string constant in every
module, walked. A name assembled at runtime is not seen; review covers that.
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = PROJECT_ROOT / "src" / "l3io" / "wp" / "backup"
NAME = "MYSQL_PWD"


def _offenders(root: Path) -> list[str]:
    return sorted(
        f"{path.relative_to(root)}:{node.lineno}"
        for path in root.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if (
            (isinstance(node, ast.Constant) and isinstance(node.value, str))
            and NAME in node.value
        )
        or (isinstance(node, ast.Name) and node.id == NAME)
    )


def test_the_package_has_source_to_check() -> None:
    assert len(list(PACKAGE.rglob("*.py"))) > 10, f"no Python source under {PACKAGE}"


def test_no_module_names_mysql_pwd() -> None:
    offenders = _offenders(PACKAGE)
    assert not offenders, f"MYSQL_PWD is never set by this package: {offenders}"


def test_the_guard_fires_on_an_env_mapping_in_a_nested_module(tmp_path: Path) -> None:
    nested = tmp_path / "cli" / "deep"
    nested.mkdir(parents=True)
    (nested / "x.py").write_text(
        "import os\nenv = {**os.environ, 'MYSQL_PWD': p}\n", encoding="utf-8"
    )
    (tmp_path / "y.py").write_text(
        "import os\nos.environ['MYSQL_PWD'] = p\n", encoding="utf-8"
    )
    assert len(_offenders(tmp_path)) == 2
