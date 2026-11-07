"""FR-5 reach (E004 closure AD-1; E008-S01-001) — the default exclusion set is applied.

Two halves. (1) ``cli/backup.default_exclusion_policy()``, the one place the
CLI with no exclusion flag and a library request without ``exclusion`` get
their set from, is Epic 4's default set — not ``INCLUDE_EVERYTHING``, and its
``source`` is ``"default"``. (2) ``ExclusionSet`` and
``LoggedExclusionPolicy`` each have a caller in ``cli/``: an AST scan of every
module under ``src/l3io/wp/backup/cli/`` (with a non-empty scope assertion)
finds a call of each, bare or through an attribute chain.

Not covered: a caller that builds the set and then discards it.
"""

from __future__ import annotations

import ast
import shutil
from pathlib import Path

import pytest

from l3io.wp.backup.cli import backup as backup_cli
from l3io.wp.backup.core.exclusions import DEFAULT_EXCLUSIONS
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.exclusion import INCLUDE_EVERYTHING

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CLI = PROJECT_ROOT / "src" / "l3io" / "wp" / "backup" / "cli"
REQUIRED = ("ExclusionSet", "LoggedExclusionPolicy")


def _called_names(root: Path) -> set[str]:
    """Every name called in a module under ``root`` (``X(`` or ``a.X(``).

    A classmethod call (``ExclusionSet.default()``) counts as a call of its
    class: the attribute's object name is collected too.
    """
    names: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
                if isinstance(func.value, ast.Name):
                    names.add(func.value.id)
    return names


def _uncalled(root: Path) -> list[str]:
    called = _called_names(root)
    return [name for name in REQUIRED if name not in called]


def test_the_scan_reaches_cli() -> None:
    files = {p.name for p in CLI.rglob("*.py")}
    assert {"backup.py", "restore.py", "__init__.py"} <= files
    assert _called_names(CLI)


def test_the_default_policy_is_epic_4s_default_set() -> None:
    policy = backup_cli.default_exclusion_policy()
    assert policy is not INCLUDE_EVERYTHING
    assert policy.source == "default"
    assert tuple(policy.patterns) == DEFAULT_EXCLUSIONS
    assert DEFAULT_EXCLUSIONS, "the default set is empty: nothing would be excluded"


def test_a_request_without_exclusion_gets_the_default(tmp_path: Path) -> None:
    request = backup_cli.BackupRequest(
        site_root=tmp_path,
        scope=BackupScope.FILES,
        target=None,
        wp_config=None,
        credentials=None,
        cipher=None,
    )
    assert request.exclusion.source == "default"


def test_the_exclusion_types_have_callers_in_cli() -> None:
    assert _uncalled(CLI) == []


def test_reverting_the_default_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """The scope attack on half (1): a revert to INCLUDE_EVERYTHING fails."""
    monkeypatch.setattr(
        backup_cli, "default_exclusion_policy", lambda: INCLUDE_EVERYTHING
    )
    with pytest.raises(AssertionError):
        test_the_default_policy_is_epic_4s_default_set()


def test_a_cli_without_the_calls_is_reported(tmp_path: Path) -> None:
    """The scope attack on half (2): a copy of ``cli/`` with the calls removed."""
    copy = tmp_path / "cli"
    shutil.copytree(CLI, copy, ignore=shutil.ignore_patterns("__pycache__"))
    for path in copy.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for name in REQUIRED:
            text = text.replace(f"{name}(", "_removed(").replace(
                f"{name}.", "_removed."
            )
        path.write_text(text, encoding="utf-8")
    assert _uncalled(copy) == list(REQUIRED)
