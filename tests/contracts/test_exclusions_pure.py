"""FR-5, AD-1 — ``core/exclusions.py`` uses ``pathspec``'s string matcher only.

``tools/check_purity.py`` and import-linter see the import of ``pathspec`` but
not into it; its tree-walking helpers read the filesystem. This scans the
module's AST for any reference to them — an attribute, an imported name or a
bare name — under any spelling ``ast`` exposes.

The forbidden set is derived from ``pathspec`` itself: every public name on
``PathSpec``/``GitIgnoreSpec`` and in ``pathspec``/``pathspec.util`` that says
it walks a tree, asserted to hold the ones the story names, so a helper added
in a later ``pathspec`` release is covered without editing this file.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pathspec
import pathspec.util

from l3io.wp.backup.core import exclusions

MODULE = Path(exclusions.__file__)
NAMED = {"match_tree", "match_tree_entries", "match_tree_files", "check_tree_files"}


def _tree_walkers() -> frozenset[str]:
    names = set(dir(pathspec)) | set(dir(pathspec.util))
    names |= set(dir(pathspec.PathSpec)) | set(dir(pathspec.GitIgnoreSpec))
    return frozenset(n for n in names if not n.startswith("_") and "tree" in n.lower())


def references(source: str, forbidden: frozenset[str]) -> list[str]:
    """Every forbidden name ``source`` reaches, in any form."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr in forbidden:
            found.append(node.attr)
        elif isinstance(node, ast.Name) and node.id in forbidden:
            found.append(node.id)
        elif isinstance(node, ast.ImportFrom):
            found += [a.name for a in node.names if a.name in forbidden]
        elif isinstance(node, ast.Constant) and node.value in forbidden:
            found.append(str(node.value))
    return sorted(found)


def test_the_forbidden_set_holds_the_named_helpers() -> None:
    assert _tree_walkers() >= NAMED


def test_core_exclusions_reaches_no_tree_walker() -> None:
    assert references(MODULE.read_text(encoding="utf-8"), _tree_walkers()) == []


def test_the_scan_fires_on_each_spelling() -> None:
    planted = (
        "from pathspec import iter_tree_files\n"
        "from pathspec.util import iter_tree_entries as walk\n"
        "spec.match_tree_files(root)\n"
        "getattr(spec, 'check_tree_files')(root)\n"
        "f = match_tree\n"
    )
    assert references(planted, _tree_walkers()) == [
        "check_tree_files",
        "iter_tree_entries",
        "iter_tree_files",
        "match_tree",
        "match_tree_files",
    ]
