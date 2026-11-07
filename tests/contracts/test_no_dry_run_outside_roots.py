"""AD-4 — only the composition roots (``cli/``, ``api/``) may name the Dry Run.

The local mirror of CI's "AD-4 no dry-run conditionals" step
(``.github/workflows/ci.yml``), so the rule fails where the developer works.
The pattern and the exemption are read **from that step**, so the two cannot
drift; the CI step stays the gate of record (``test_dry_run_gate.py`` runs it).

Scope: every ``*.py`` under ``src/l3io/wp/backup/``, walked, matched line by
line with the step's case-insensitive extended regex (no word boundary, so
compound and camel spellings match), comments and docstrings included — as
``grep -i`` sees them. A match is exempt when its ``grep -rn`` output line
(``src/l3io/wp/backup/<path>:<n>:<text>``) matches the step's ``grep -vE``
exemption, which anchors it to the two roots by path (a directory merely named
``cli`` or ``api`` deeper down is not exempt), or when the line no longer
matches after the step's ``sed`` strips its allowed whole tokens.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

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


def gate() -> tuple[re.Pattern[str], re.Pattern[str], re.Pattern[str]]:
    """The step's ``grep -rniE`` pattern, ``grep -vE`` exemption and ``sed`` strip."""
    step = _step_text()
    pattern = re.search(r"grep -rniE '([^']+)'", step)
    assert pattern is not None, f"{STEP}: no grep -rniE pattern in the step"
    exempt = re.search(r"grep -vE '([^']+)'", step)
    assert exempt is not None, f"{STEP}: no grep -vE exemption in the step"
    strip = re.search(r"sed -E 's/([^/]+)//g'", step)
    assert strip is not None, f"{STEP}: no sed -E token strip in the step"
    recheck = re.search(r"grep -iE '([^']+)'", step)
    assert recheck is not None, f"{STEP}: no grep -iE re-check in the step"
    assert recheck.group(1) == pattern.group(1), f"{STEP}: the two patterns differ"
    # grep's \b is a word boundary in Python's re too.
    return (
        re.compile(pattern.group(1), re.IGNORECASE),
        re.compile(exempt.group(1)),
        re.compile(strip.group(1)),
    )


def _offenders(
    root: Path,
    pattern: re.Pattern[str],
    exempt: re.Pattern[str] | None,
    strip: re.Pattern[str] | None = None,
) -> list[str]:
    """``grep -rn`` lines under ``root`` (named as ``PREFIX``) not exempted."""
    found = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), 1):
            output = f"{PREFIX}/{relative}:{number}:{line}"
            if not pattern.search(line) or (exempt and exempt.search(output)):
                continue
            if strip and not pattern.search(strip.sub("", line)):
                continue
            found.append(f"{relative}:{number}")
    return found


def test_the_gate_is_read_from_the_workflow() -> None:
    pattern, exempt, strip = gate()
    assert pattern.search("dry_run=True")
    assert pattern.search("what_if")
    assert not pattern.search("dry-run")  # wp-cli's flag, not an identifier
    assert exempt.search(f"{PREFIX}/cli/backup.py:1:dry_run")
    assert exempt.search(f"{PREFIX}/api/x.py:1:dry_run")
    assert not exempt.search(f"{PREFIX}/core/cli/x.py:1:dry_run")
    assert not exempt.search(f"{PREFIX}/cli_helpers.py:1:dry_run")
    assert not pattern.search(strip.sub("", "outcome = 'skipped_dry_run'"))
    assert not pattern.search(strip.sub("", "see ``dry_run_adapters(...)``"))


# Every spelling BL-E002-111 / BL-E003-108 named: compound, private, camel.
SPELLINGS = [
    "self._dry_run = True",
    "if dry_run_flag:",
    "skipped_dry_run_count = 0",
    "is_skipped_dry_run = 1",
    "isDryRun = True",
    "DryRun = 1",
    "dryrun = 1",
    "whatIf = True",
    "WhatIf = 1",
    "what_if_mode = 1",
    "self._what_if = 1",
    "x = 1  # dry_run_adapters, but also if dry_run:",
]


@pytest.mark.parametrize("line", SPELLINGS)
def test_the_guard_catches_every_spelling(tmp_path: Path, line: str) -> None:
    pattern, exempt, strip = gate()
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "x.py").write_text(line + "\n", encoding="utf-8")
    assert _offenders(tmp_path, pattern, exempt, strip) == ["core/x.py:1"]


def test_the_scope_is_the_package_and_the_roots_really_name_it() -> None:
    pattern, _, _ = gate()
    modules = list(PACKAGE.rglob("*.py"))
    assert len(modules) > 10, f"no Python source under {PACKAGE}"
    # Exempting the roots hides something real, or the exemption means nothing.
    assert _offenders(PACKAGE, pattern, None)


def test_no_module_outside_the_roots_names_the_dry_run() -> None:
    pattern, exempt, strip = gate()
    offenders = _offenders(PACKAGE, pattern, exempt, strip)
    assert not offenders, (
        "only cli/ and api/ may name dry_run/what_if (AD-4); pass the root's "
        f"decision as data (span attributes, the adapter set): {offenders}"
    )


def test_the_guard_fires_outside_the_roots_at_any_depth(tmp_path: Path) -> None:
    pattern, exempt, strip = gate()
    files = {
        "cli/root.py": "dry_run = True\n",
        "api/root.py": "what_if = True\n",
        "ports/deep/p.py": "x = 1\n    def f(self, *, dry_run: bool) -> None: ...\n",
        "ports/q.py": "# what_if mode\nwhat-iffy = 1\n",
        "core/cli/x.py": "if dry_run:\n    pass\n",
        "adapters/api/y.py": "what_if = 1\n",
    }
    for relative, text in files.items():
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_text(text, encoding="utf-8")
    assert _offenders(tmp_path, pattern, exempt, strip) == [
        "adapters/api/y.py:1",
        "core/cli/x.py:1",
        "ports/deep/p.py:2",
        "ports/q.py:1",
    ]
