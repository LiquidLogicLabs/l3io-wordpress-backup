"""AD-26 — one passphrase resolution chain (E004-S01-003, F-7).

The passphrase environment variable's name, ``L3IO_WP_BACKUP_PASSPHRASE``,
appears as a whole string constant in exactly one module: ``cli/passphrase.py``,
which holds ``resolve_passphrase``. A second module reading the variable would
be a second chain. The scope is every ``*.py`` under ``src/l3io/wp/backup/``,
walked with ``rglob`` (a directory added without ``__init__.py`` is still in
it), with a scope assertion.

Counted: an ``ast.Constant`` whose value *equals* the name. Not counted, so the
next reader keeps looking: prose that mentions it inside a longer string (the
``cli/parser.py`` help text does, deliberately), and a name assembled at
runtime (``"L3IO_WP_BACKUP_" + "PASSPHRASE"``). That the CLI and ``api/`` roots
resolve through ``resolve_passphrase`` is E008-S01-001's and E006-S01-001's.
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = PROJECT_ROOT / "src" / "l3io" / "wp" / "backup"
CHAIN = PACKAGE / "cli" / "passphrase.py"
NAME = "L3IO_WP_BACKUP_PASSPHRASE"


def _holders(root: Path) -> list[Path]:
    """Every module under ``root`` spelling :data:`NAME` as a whole constant."""
    return sorted(
        path
        for path in root.rglob("*.py")
        if any(
            isinstance(node, ast.Constant) and node.value == NAME
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        )
    )


def test_the_walk_reaches_the_chain() -> None:
    assert CHAIN in sorted(PACKAGE.rglob("*.py")), f"AD-26 scope missing {CHAIN}"


def test_only_the_chain_names_the_passphrase_variable() -> None:
    assert _holders(PACKAGE) == [CHAIN], "AD-26 — a second passphrase chain"


def test_a_planted_constant_is_found_and_prose_is_not(tmp_path: Path) -> None:
    deep = tmp_path / "nested" / "no_init"
    deep.mkdir(parents=True)
    (deep / "second_chain.py").write_text(
        f"import os\nVALUE = os.environ.get({NAME!r})\n", encoding="utf-8"
    )
    (tmp_path / "parser.py").write_text(
        f"HELP = 'The passphrase comes from {NAME} or a prompt.'\n", encoding="utf-8"
    )
    assert _holders(tmp_path) == [deep / "second_chain.py"]
