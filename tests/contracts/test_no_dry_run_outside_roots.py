"""AD-4 — only the composition roots (``cli/``, ``api/``) may name the Dry Run.

The local mirror of CI's "AD-4 no dry-run conditionals" step
(``.github/workflows/ci.yml``), so the rule fails where the developer works.
The pattern and the excluded directory names are read **from that step**, so
the two cannot drift; the CI step stays the gate of record.

Scope: every ``*.py`` under ``src/l3io/wp/backup/``, walked, minus any
directory named as the step's ``--exclude-dir`` (at any depth, as ``grep``
does). Matched line by line with the step's extended regex, comments and
docstrings included — as ``grep`` sees them.
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = PROJECT_ROOT / "src" / "l3io" / "wp" / "backup"
WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"
STEP = "AD-4 no dry-run conditionals"


def _step_text() -> str:
    text = WORKFLOW.read_text(encoding="utf-8")
    start = text.index(f"name: {STEP}")
    following = text.find("- name:", start + 1)
    return text[start : following if following != -1 else len(text)]


def gate() -> tuple[re.Pattern[str], frozenset[str]]:
    """The step's ``grep -E`` pattern and its ``--exclude-dir`` names."""
    step = _step_text()
    pattern = re.search(r"grep -rnE '([^']+)'", step)
    assert pattern is not None, f"{STEP}: no grep -rnE pattern in the step"
    excluded = frozenset(re.findall(r"--exclude-dir=([\w.-]+)", step))
    # grep's \b is a word boundary in Python's re too.
    return re.compile(pattern.group(1)), excluded


def _offenders(
    root: Path, pattern: re.Pattern[str], excluded: frozenset[str]
) -> list[str]:
    return sorted(
        f"{path.relative_to(root)}:{number}"
        for path in root.rglob("*.py")
        if not excluded & set(path.relative_to(root).parts[:-1])
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line)
    )


def test_the_gate_is_read_from_the_workflow() -> None:
    pattern, excluded = gate()
    assert excluded == {"cli", "api"}
    assert pattern.search("dry_run=True")
    assert pattern.search("what_if")
    assert not pattern.search("dry_running")


def test_the_scope_is_the_package_and_the_roots_really_name_it() -> None:
    pattern, _ = gate()
    modules = list(PACKAGE.rglob("*.py"))
    assert len(modules) > 10, f"no Python source under {PACKAGE}"
    # Excluding the roots hides something real, or the exclusion means nothing.
    assert _offenders(PACKAGE, pattern, frozenset())


def test_no_module_outside_the_roots_names_the_dry_run() -> None:
    pattern, excluded = gate()
    offenders = _offenders(PACKAGE, pattern, excluded)
    assert not offenders, (
        "only cli/ and api/ may name dry_run/what_if (AD-4); pass the root's "
        f"decision as data (span attributes, the adapter set): {offenders}"
    )


def test_the_guard_fires_outside_the_roots_at_any_depth(tmp_path: Path) -> None:
    pattern, excluded = gate()
    (tmp_path / "cli").mkdir()
    (tmp_path / "ports" / "deep").mkdir(parents=True)
    (tmp_path / "cli" / "root.py").write_text("dry_run = True\n", encoding="utf-8")
    (tmp_path / "ports" / "deep" / "p.py").write_text(
        "x = 1\n    def f(self, *, dry_run: bool) -> None: ...\n", encoding="utf-8"
    )
    (tmp_path / "ports" / "q.py").write_text(
        "# what_if mode\nwhat_iffy = 1\n", encoding="utf-8"
    )
    assert _offenders(tmp_path, pattern, excluded) == [
        "ports/deep/p.py:2",
        "ports/q.py:1",
    ]
