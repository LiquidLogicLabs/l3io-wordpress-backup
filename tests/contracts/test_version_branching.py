"""AD-23 — no module outside ``domain/archive_format.py`` branches on format version.

Every module under ``src/l3io/wp/backup/`` (walked recursively, asserted
non-empty) except the owner is parsed, and any **decision** that references
``format_version`` fails: an ``if``/``while``/``assert`` test, a comprehension
``if``, a conditional expression's test, a comparison anywhere, or a ``match``
subject. A reference is the name ``format_version``, an attribute of that name,
or the string ``"format_version"`` (a ``dict`` lookup). Reading the value to log
or display it — a keyword argument, an assignment, ``format_label(...)`` — is
allowed. Not covered: a branch on a value copied out of ``format_version`` into
another name first; review covers that.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = PROJECT_ROOT / "src" / "l3io" / "wp" / "backup"
OWNER = PACKAGE / "domain" / "archive_format.py"
FIELD = "format_version"


def _mentions(node: ast.AST) -> bool:
    for inner in ast.walk(node):
        if isinstance(inner, ast.Name) and inner.id == FIELD:
            return True
        if isinstance(inner, ast.Attribute) and inner.attr == FIELD:
            return True
        if isinstance(inner, ast.Constant) and inner.value == FIELD:
            return True
    return False


def _decisions(tree: ast.AST) -> list[ast.AST]:
    found: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.If | ast.While | ast.Assert | ast.IfExp):
            found.append(node.test)
        elif isinstance(node, ast.comprehension):
            found.extend(node.ifs)
        elif isinstance(node, ast.Compare):
            found.append(node)
        elif isinstance(node, ast.Match):
            found.append(node.subject)
    return found


def _branches(root: Path, owner: Path) -> list[str]:
    return sorted(
        {
            f"{path.relative_to(root)}:{decision.lineno}"
            for path in sorted(root.rglob("*.py"))
            if path != owner
            for decision in _decisions(ast.parse(path.read_text(encoding="utf-8")))
            if _mentions(decision)
        }
    )


def test_the_scope_is_the_package() -> None:
    modules = sorted(PACKAGE.rglob("*.py"))
    assert OWNER in modules
    assert len(modules) > 1


def test_only_the_owner_branches_on_format_version() -> None:
    hits = _branches(PACKAGE, OWNER)
    assert not hits, f"AD-23 — branch on {FIELD} outside {OWNER.name}: {hits}"


@pytest.mark.parametrize(
    "body",
    [
        "if view.format_version == 1:\n    pass\n",
        "while format_version:\n    pass\n",
        "x = 1 if m.format_version else 0\n",
        "ok = m.format_version in {1}\n",
        "match m.format_version:\n    case 1:\n        pass\n",
        "y = [a for a in b if a.format_version]\n",
        "if doc['format_version'] > 1:\n    pass\n",
        "assert view.format_version is not None\n",
    ],
)
def test_the_walk_sees_a_branch_anywhere(tmp_path: Path, body: str) -> None:
    """Scope attack: nested, no ``__init__``, and a file named like the owner."""
    root = tmp_path / "backup"
    (root / "domain").mkdir(parents=True)
    owner = root / "domain" / "archive_format.py"
    owner.write_text(body, encoding="utf-8")
    (root / "nested" / "deep").mkdir(parents=True)
    (root / "nested" / "deep" / "archive_format.py").write_text(body, encoding="utf-8")
    hits = _branches(root, owner)
    assert hits == [f"nested/deep/archive_format.py:{hits[0].rsplit(':', 1)[1]}"]


@pytest.mark.parametrize(
    "body",
    [
        "log.info('x', format_version=view.format_version)\n",
        "label = format_label(result.format_version)\n",
        "Manifest(format_version=FORMAT_VERSION)\n",
    ],
)
def test_reading_to_log_or_display_is_allowed(tmp_path: Path, body: str) -> None:
    (tmp_path / "m.py").write_text(body, encoding="utf-8")
    assert _branches(tmp_path, tmp_path / "owner.py") == []
