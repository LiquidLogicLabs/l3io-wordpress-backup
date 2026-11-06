"""FR-27 — the README's external-tools table is generated.

Runs ``tools/gen_tool_docs.py --check`` inside the existing pytest job (no CI
step of its own), and proves the check fires on planted drift in a copy of the
README. Which tools must have a row is
``tests/contracts/test_tool_requirements_documented.py``'s concern.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "gen_tool_docs.py"
README = ROOT / "README.md"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - a fixed in-repo script
        [sys.executable, str(TOOL), *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
    )


def test_the_readme_is_current() -> None:
    result = _run("--check")
    assert result.returncode == 0, result.stdout + result.stderr


def test_check_fails_on_planted_drift(tmp_path: Path) -> None:
    copy = tmp_path / "README.md"
    text = README.read_text(encoding="utf-8")
    planted = text.replace("| `age` |", "| `agee` |", 1)
    assert planted != text, "the planted drift changed nothing"
    copy.write_text(planted, encoding="utf-8")
    result = _run("--check", "--readme", str(copy))
    assert result.returncode == 1
    assert "README.md is stale" in result.stdout
    assert copy.read_text(encoding="utf-8") == planted, "--check wrote the file"


def test_a_missing_block_is_an_error(tmp_path: Path) -> None:
    copy = tmp_path / "README.md"
    copy.write_text("# no markers\n", encoding="utf-8")
    result = _run("--check", "--readme", str(copy))
    assert result.returncode != 0
    assert "no well-formed" in result.stderr
