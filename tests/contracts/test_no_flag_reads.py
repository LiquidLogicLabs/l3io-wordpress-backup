"""AD-25 — only the restore roots read a restore flag off the parsed namespace.

**Rule.** No module reads ``cleanfirst``, ``drop_database`` or ``force`` off an
object except the two permitted readers: ``cli/__init__.py`` (its pre-Run
``check_restore_arguments(...)`` call) and ``cli/restore.py`` (the restore
root). Everyone else receives the ``(RestoreMode, OverrideSet)`` that
``domain/flag_map.py::resolve_flags`` returns, so there is no second mapping
and no ``if args.force``. A read is an ``ast.Attribute`` in load context whose
``attr`` is one of those names and which is not the ``func`` of an
``ast.Call`` — so ``DatabaseCommand.drop_database(...)`` is a call, not a read.

**Scope, derived.** The names are ``COVERED_FLAGS`` minus ``mode`` (``backup``
reads ``args.mode`` for its Backup Scope, which this rule must not forbid), so
a dimension added to the table is covered without editing this test. The
modules are every ``*.py`` found by walking ``src/l3io/wp/backup/``.

**Not covered:** ``getattr(args, "force")``, ``vars(args)["force"]``, and a
namespace passed whole to a helper that reads it under another name.
"""

from __future__ import annotations

import ast
import shutil
from collections.abc import Iterable
from pathlib import Path

import pytest

from l3io.wp.backup.domain import flag_map

PACKAGE = Path(__file__).resolve().parents[2] / "src" / "l3io" / "wp" / "backup"
PERMITTED = frozenset({"cli/__init__.py", "cli/restore.py"})


def restore_flag_dests(covered: Iterable[str]) -> frozenset[str]:
    """The flag ``dest``s whose value only a restore root may read."""
    return frozenset(covered) - {"mode"}


def flag_reads(
    root: Path, dests: frozenset[str], permitted: frozenset[str] = PERMITTED
) -> list[str]:
    """``path:line attr`` for every read of ``dests`` outside ``permitted``."""
    found: list[str] = []
    modules = sorted(root.rglob("*.py"))
    assert modules, f"no module found under {root}"
    for path in modules:
        relative = path.relative_to(root).as_posix()
        if relative in permitted:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        called = {
            id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)
        }
        found += [
            f"{relative}:{node.lineno} {node.attr}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Load)
            and node.attr in dests
            and id(node) not in called
        ]
    return found


DESTS = restore_flag_dests(flag_map.COVERED_FLAGS)


def test_the_scope_is_the_table_less_mode() -> None:
    assert {"cleanfirst", "drop_database", "force"} <= DESTS
    assert "mode" not in DESTS


def test_the_permitted_readers_exist() -> None:
    for relative in PERMITTED:
        assert (PACKAGE / relative).is_file(), relative


def test_no_module_but_the_restore_roots_reads_a_restore_flag() -> None:
    assert flag_reads(PACKAGE, DESTS) == []


def test_the_rule_sees_the_reads_it_permits() -> None:
    """With no reader permitted, ``cli/__init__.py``'s reads are found.

    Otherwise the rule could pass vacuously over a walk that parses nothing.
    """
    reads = flag_reads(PACKAGE, DESTS, permitted=frozenset())
    assert {r.split()[1] for r in reads if r.startswith("cli/__init__.py:")} == DESTS


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    copy = tmp_path / "backup"
    shutil.copytree(PACKAGE, copy, ignore=shutil.ignore_patterns("__pycache__"))
    return copy


def _plant(tree: Path, relative: str, code: str) -> None:
    path = tree / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    path.write_text(existing + "\n\n" + code + "\n", encoding="utf-8")


@pytest.mark.parametrize(
    ("relative", "code", "attr"),
    [
        (
            "adapters/database.py",
            "def _planted(args):\n    return args.drop_database",
            "drop_database",
        ),
        ("core/restore.py", "def _planted(ns):\n    return ns.force", "force"),
        (
            "cli/backup.py",
            "def _planted(args):\n    return args.cleanfirst",
            "cleanfirst",
        ),
        # A module the walk must find without being listed anywhere.
        (
            "newlayer/fresh.py",
            "def _planted(args):\n    return args.force",
            "force",
        ),
    ],
    ids=["adapters", "core-other-name", "cli-backup", "new-module"],
)
def test_a_planted_read_fails_the_rule(
    tree: Path, relative: str, code: str, attr: str
) -> None:
    _plant(tree, relative, code)
    reads = flag_reads(tree, DESTS)
    assert len(reads) == 1
    assert reads[0].startswith(f"{relative}:")
    assert reads[0].endswith(f" {attr}")


def test_a_command_call_is_not_a_read(tree: Path) -> None:
    _plant(
        tree,
        "adapters/database.py",
        'def _planted(cmd, location):\n    cmd.drop_database(location, confirm="x")',
    )
    assert flag_reads(tree, DESTS) == []


def test_a_dimension_added_to_the_table_is_covered(tree: Path) -> None:
    _plant(tree, "core/restore.py", "def _planted(args):\n    return args.new_flag")
    assert flag_reads(tree, DESTS) == []
    widened = restore_flag_dests(flag_map.COVERED_FLAGS | {"new_flag"})
    assert flag_reads(tree, widened) == ["core/restore.py:" + _line(tree) + " new_flag"]


def _line(tree: Path) -> str:
    lines = (tree / "core" / "restore.py").read_text(encoding="utf-8").splitlines()
    return str(next(i for i, t in enumerate(lines, 1) if "args.new_flag" in t))
