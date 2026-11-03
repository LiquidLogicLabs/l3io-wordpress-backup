"""AD-4 — only the composition roots (``cli/``, ``api/``) may name the Dry Run.

The local mirror of CI's "AD-4 no dry-run conditionals" step
(``.github/workflows/ci.yml``), so the rule fails where the developer works.
The pattern and the exemption are read **from that step**, so the two cannot
drift; the CI step stays the gate of record (``test_dry_run_gate.py`` runs it).

Scope: every ``*.py`` under ``src/l3io/wp/backup/``, walked, matched line by
line with the step's extended regex, comments and docstrings included — as
``grep`` sees them. A match is exempt only when its ``grep -rn`` output line
(``src/l3io/wp/backup/<path>:<n>:<text>``) matches the step's ``grep -vE``
exemption, which anchors it to the two roots by path; a directory merely named
``cli`` or ``api`` deeper down is not exempt.
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PREFIX = "src/l3io/wp/backup"
PACKAGE = PROJECT_ROOT / PREFIX
WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"
STEP = "AD-4 no dry-run conditionals"


def _step_text() -> str:
    text = WORKFLOW.read_text(encoding="utf-8")
    start = text.index(f"name: {STEP}")
    following = text.find("- name:", start + 1)
    return text[start : following if following != -1 else len(text)]


def gate() -> tuple[re.Pattern[str], re.Pattern[str]]:
    """The step's ``grep -rnE`` pattern and its ``grep -vE`` exemption."""
    step = _step_text()
    pattern = re.search(r"grep -rnE '([^']+)'", step)
    assert pattern is not None, f"{STEP}: no grep -rnE pattern in the step"
    exempt = re.search(r"grep -vE '([^']+)'", step)
    assert exempt is not None, f"{STEP}: no grep -vE exemption in the step"
    # grep's \b is a word boundary in Python's re too.
    return re.compile(pattern.group(1)), re.compile(exempt.group(1))


def _offenders(
    root: Path, pattern: re.Pattern[str], exempt: re.Pattern[str] | None
) -> list[str]:
    """``grep -rn`` lines under ``root`` (named as ``PREFIX``) not ``exempt``."""
    found = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), 1):
            output = f"{PREFIX}/{relative}:{number}:{line}"
            if pattern.search(line) and not (exempt and exempt.search(output)):
                found.append(f"{relative}:{number}")
    return found


def test_the_gate_is_read_from_the_workflow() -> None:
    pattern, exempt = gate()
    assert pattern.search("dry_run=True")
    assert pattern.search("what_if")
    assert not pattern.search("dry_running")
    assert exempt.search(f"{PREFIX}/cli/backup.py:1:dry_run")
    assert exempt.search(f"{PREFIX}/api/x.py:1:dry_run")
    assert not exempt.search(f"{PREFIX}/core/cli/x.py:1:dry_run")
    assert not exempt.search(f"{PREFIX}/cli_helpers.py:1:dry_run")


def test_the_scope_is_the_package_and_the_roots_really_name_it() -> None:
    pattern, _ = gate()
    modules = list(PACKAGE.rglob("*.py"))
    assert len(modules) > 10, f"no Python source under {PACKAGE}"
    # Exempting the roots hides something real, or the exemption means nothing.
    assert _offenders(PACKAGE, pattern, None)


def test_no_module_outside_the_roots_names_the_dry_run() -> None:
    pattern, exempt = gate()
    offenders = _offenders(PACKAGE, pattern, exempt)
    assert not offenders, (
        "only cli/ and api/ may name dry_run/what_if (AD-4); pass the root's "
        f"decision as data (span attributes, the adapter set): {offenders}"
    )


def test_the_guard_fires_outside_the_roots_at_any_depth(tmp_path: Path) -> None:
    pattern, exempt = gate()
    files = {
        "cli/root.py": "dry_run = True\n",
        "api/root.py": "what_if = True\n",
        "ports/deep/p.py": "x = 1\n    def f(self, *, dry_run: bool) -> None: ...\n",
        "ports/q.py": "# what_if mode\nwhat_iffy = 1\n",
        "core/cli/x.py": "if dry_run:\n    pass\n",
        "adapters/api/y.py": "what_if = 1\n",
    }
    for relative, text in files.items():
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_text(text, encoding="utf-8")
    assert _offenders(tmp_path, pattern, exempt) == [
        "adapters/api/y.py:1",
        "core/cli/x.py:1",
        "ports/deep/p.py:2",
        "ports/q.py:1",
    ]
