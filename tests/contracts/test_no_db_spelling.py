"""FR-2 — the domain value is ``database``; ``db`` is spelled only at the CLI edge.

Two modules may spell it: ``cli/parser.py`` declares the frozen token among
``--mode``'s ``choices`` (AD-22), and ``cli/scope.py`` holds the one
translation to ``BackupScope.DATABASE``. Every other module of the package —
``cli/``'s other modules included — is walked, and any ``"db"`` string literal in
it fails. The set is derived from the package tree, so a subpackage added
later (``api/``, say) is covered the moment it exists. Literals are found
through ``ast``, so either quote style is caught, and ``db_name``-style
identifiers are not.

**Not covered**, so the next reader keeps looking: a ``"db"`` built at run
time (``"d" + "b"``, a format string), an attribute or member *named* ``db``
or ``DB``, and the token arriving through a non-literal such as a config file.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import l3io.wp.backup as package

PACKAGE_ROOT = Path(package.__file__).resolve().parent
CLI_DIR = "cli"
#: The one translation, and the parser that declares the token it translates.
TRANSLATION = Path(CLI_DIR) / "scope.py"
DECLARATION = Path(CLI_DIR) / "parser.py"
ALLOWED = frozenset({TRANSLATION, DECLARATION})
FORBIDDEN = "db"


def _modules(root: Path) -> list[Path]:
    """Every module under ``root`` except the declaration and the translation."""
    return sorted(
        path for path in root.rglob("*.py") if path.relative_to(root) not in ALLOWED
    )


def _hits(root: Path) -> list[str]:
    hits = []
    for path in _modules(root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        hits.extend(
            f"{path.relative_to(root)}:{node.lineno}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and node.value == FORBIDDEN
        )
    return hits


def test_only_the_translation_spells_a_scope_db() -> None:
    assert _hits(PACKAGE_ROOT) == [], (
        "the domain value is 'database'; translate 'db' only in cli/scope.py"
    )


@pytest.mark.parametrize("allowed", sorted(ALLOWED))
def test_each_exempt_module_exists_and_spells_db(allowed: Path) -> None:
    """The exemptions are not vacuous: each file named exists and holds ``db``."""
    tree = ast.parse((PACKAGE_ROOT / allowed).read_text(encoding="utf-8"))
    assert any(
        isinstance(node, ast.Constant) and node.value == FORBIDDEN
        for node in ast.walk(tree)
    )


def test_the_walked_set_contains_every_layer_and_is_non_empty() -> None:
    """The scope half: the layers the rule names are walked, and hold modules."""
    modules = _modules(PACKAGE_ROOT)
    walked = {path.relative_to(PACKAGE_ROOT).parts[0] for path in modules}
    for layer in ("domain", "core", "ports", "adapters", CLI_DIR):
        assert layer in walked, f"{layer}/ is missing from the walked set"
        assert any((PACKAGE_ROOT / layer).glob("*.py"))
    assert not {PACKAGE_ROOT / allowed for allowed in ALLOWED} & set(modules)


def test_a_planted_literal_anywhere_but_the_translation_is_found(
    tmp_path: Path,
) -> None:
    """A subpackage nobody listed and a third ``cli/`` module are both walked."""
    (tmp_path / "cli").mkdir()
    (tmp_path / "cli" / "scope.py").write_text('MODE = "db"\n')
    (tmp_path / "cli" / "parser.py").write_text('CHOICES = ["db"]\n')
    (tmp_path / "cli" / "backup.py").write_text("\n\nscope = {'db': 1}\n")
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "run.py").write_text("x = 'db'\ny = \"db_name\"\n")
    assert _hits(tmp_path) == [
        str(Path("api") / "run.py") + ":1",
        str(Path("cli") / "backup.py") + ":3",
    ]
