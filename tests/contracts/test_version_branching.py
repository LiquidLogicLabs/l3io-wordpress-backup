"""AD-23 — no module outside ``domain/archive_format.py`` compares format versions.

Every module under ``src/l3io/wp/backup/`` (walked recursively, asserted
non-empty) except the owner is parsed, and any **decision** that references a
version fails: an ``if``/``while``/``assert`` test, a comprehension ``if``, a
conditional expression's test, a comparison anywhere, or a ``match`` subject
or ``case`` pattern.

A version reference is (AD-23 as amended 2026-11-07, owner decision 13):

- the name ``format_version``, an attribute of that name, or the string
  ``"format_version"`` (a ``dict`` lookup);
- a version constant the owner declares — every module-level name in
  ``domain/archive_format.py`` containing ``FORMAT_VERSION``, read from the
  owner's source, never listed here (``FORMAT_VERSION``,
  ``SUPPORTED_FORMAT_VERSIONS``), as a name or an attribute;
- a version literal: a string such as ``"v0"`` or ``"v1"``.

Payload presence (``view.payload is None``, a member in the index) is a layout
fact and is allowed. Reading a version to log or display it — a keyword
argument, an assignment, ``format_label(...)`` — is allowed. Not covered: a
branch on a value copied out of ``format_version`` into another name first;
review covers that.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = PROJECT_ROOT / "src" / "l3io" / "wp" / "backup"
OWNER = PACKAGE / "domain" / "archive_format.py"
FIELD = "format_version"
VERSION_LITERAL = re.compile(r"v\d+")


def _version_constants(owner: Path) -> frozenset[str]:
    """The owner's module-level names that hold a format version."""
    names: set[str] = set()
    for node in ast.parse(owner.read_text(encoding="utf-8")).body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        names.update(
            t.id
            for t in targets
            if isinstance(t, ast.Name) and "FORMAT_VERSION" in t.id
        )
    return frozenset(names)


CONSTANTS = _version_constants(OWNER)


def _mentions(node: ast.AST, constants: frozenset[str] = CONSTANTS) -> bool:
    names = constants | {FIELD}
    for inner in ast.walk(node):
        if isinstance(inner, ast.Name) and inner.id in names:
            return True
        if isinstance(inner, ast.Attribute) and inner.attr in names:
            return True
        if (
            isinstance(inner, ast.Constant)
            and isinstance(inner.value, str)
            and (inner.value == FIELD or VERSION_LITERAL.fullmatch(inner.value))
        ):
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
            found.extend(case.pattern for case in node.cases)
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


def test_the_version_constants_are_read_from_the_owner() -> None:
    # Derived, not listed: a constant renamed or added in the owner is covered.
    assert {"FORMAT_VERSION", "SUPPORTED_FORMAT_VERSIONS"} <= CONSTANTS


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
        # Version constants, bare and through the module (owner decision 13).
        "if version == FORMAT_VERSION:\n    pass\n",
        "ok = v in archive_format.SUPPORTED_FORMAT_VERSIONS\n",
        "assert n <= FORMAT_VERSION\n",
        # Version literals.
        "if format_label(x) == 'v0':\n    pass\n",
        "legacy = label != 'v1'\n",
        "y = 1 if lbl == 'v2' else 0\n",
        "match label:\n    case 'v0':\n        pass\n",
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
        # Payload presence is a layout fact (AD-23 amended 2026-11-07).
        "if view.payload is None:\n    pass\n",
        "if MANIFEST_MEMBER in index.names:\n    pass\n",
        "rules = _v0 if view.payload is None else _v1\n",
        # A string that is not a version label.
        "if mode == 'verify':\n    pass\n",
    ],
)
def test_reading_to_log_or_display_is_allowed(tmp_path: Path, body: str) -> None:
    (tmp_path / "m.py").write_text(body, encoding="utf-8")
    assert _branches(tmp_path, tmp_path / "owner.py") == []
