"""BL-E007-005: ``wp-config.php`` and ``wp-settings.php`` are spelled once.

``domain/archive_format.py`` declares ``WP_CONFIG_NAME`` and
``WP_SETTINGS_NAME``; every other module under ``src/l3io/wp/backup/``
(walked, asserted non-empty) imports them. A violation is a name bound to the
bare literal: an assignment, an annotated assignment, or a member of a tuple,
set or frozenset so bound.

**Not covered:** the literal inside a longer string (messages, docstrings, an
f-string), which names the file for a reader rather than defining it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[2] / "src" / "l3io" / "wp" / "backup"
OWNER = PACKAGE / "domain" / "archive_format.py"
NAMES = frozenset({"wp-config.php", "wp-settings.php"})


def _literals(value: ast.expr | None) -> list[str]:
    if value is None:
        return []
    found: list[str] = []
    for node in ast.walk(value):
        if isinstance(node, ast.Constant) and node.value in NAMES:
            found.append(str(node.value))
    return found


def violations(source: str, name: str) -> list[str]:
    hits: list[str] = []
    for node in ast.walk(ast.parse(source)):
        value = None
        if isinstance(node, ast.Assign | ast.AnnAssign):
            value = node.value
        hits += [f"{name}:{node.lineno} {lit}" for lit in _literals(value)]
    return hits


MODULES = sorted(p for p in PACKAGE.rglob("*.py") if p != OWNER)


def test_the_scope_is_found() -> None:
    assert len(MODULES) > 50
    assert OWNER.is_file()


def test_the_owner_declares_both() -> None:
    hits = violations(OWNER.read_text(encoding="utf-8"), OWNER.name)
    assert {h.split()[-1] for h in hits} == NAMES


@pytest.mark.parametrize("path", MODULES, ids=lambda p: str(p.relative_to(PACKAGE)))
def test_no_other_module_spells_them(path: Path) -> None:
    assert violations(path.read_text(encoding="utf-8"), path.name) == []


def test_a_planted_definition_is_found() -> None:
    planted = '_C = "wp-config.php"\nM: tuple[str, ...] = ("x", "wp-settings.php")\n'
    assert violations(planted, "planted.py") == [
        "planted.py:1 wp-config.php",
        "planted.py:2 wp-settings.php",
    ]
