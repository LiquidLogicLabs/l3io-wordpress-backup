"""AD-4 — CI's "AD-4 no dry-run conditionals" step is proven to fire.

``test_no_dry_run_outside_roots.py`` mirrors the step's pattern in Python; this
test runs the step **itself**: its ``run:`` script, read from
``.github/workflows/ci.yml`` with PyYAML, executed by ``bash`` with the same
``grep`` the runner uses, over a temporary tree laid out like the repository.
A ``core/x.py`` holding ``if dry_run:`` must fail it; a tree whose only
mention is under ``cli/`` or ``api/`` must pass, and so must the real
repository. The exemption itself is attacked: a directory merely *named*
``cli`` or ``api`` below the package root (``core/cli/x.py``) is not a
composition root and must fail the step, as must a mention in a module whose
own name starts with a root's (``cli_helpers.py``).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"
STEP = "AD-4 no dry-run conditionals"
PACKAGE = Path("src/l3io/wp/backup")

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("grep") is None,
    reason="the step is a bash script over grep",
)


def step_script() -> str:
    """The step's ``run:`` text, found by name in any job."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    scripts = [
        step["run"]
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if step.get("name") == STEP
    ]
    assert len(scripts) == 1, f"{WORKFLOW} has {len(scripts)} steps named {STEP!r}"
    script: str = scripts[0]
    return script


def run_gate(root: Path) -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    assert bash is not None
    return subprocess.run(  # noqa: S603 - the workflow's own script, fixed
        [bash, "-e", "-c", step_script()],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


def _tree(root: Path, files: dict[str, str]) -> Path:
    for relative, text in files.items():
        path = root / PACKAGE / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def test_the_step_fires_on_a_conditional_in_core(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {
            "cli/root.py": "dry_run = True\n",
            "core/x.py": "def plan(flag):\n    if dry_run:\n        pass\n",
        },
    )
    completed = run_gate(root)
    assert completed.returncode != 0
    assert "core/x.py" in completed.stdout
    assert "cli/root.py" not in completed.stdout


@pytest.mark.parametrize("where", ["ports/deep/p.py", "adapters/a.py", "domain/d.py"])
def test_the_step_fires_at_any_depth_outside_the_roots(
    tmp_path: Path, where: str
) -> None:
    completed = run_gate(_tree(tmp_path, {where: "what_if = False\n"}))
    assert completed.returncode != 0
    assert where in completed.stdout


@pytest.mark.parametrize(
    "where",
    ["core/cli/x.py", "adapters/api/x.py", "ports/cli/api/x.py", "cli_helpers.py"],
)
def test_a_directory_named_like_a_root_is_not_exempt(
    tmp_path: Path, where: str
) -> None:
    root = _tree(
        tmp_path,
        {where: "if dry_run:\n    pass\n", "cli/root.py": "dry_run = True\n"},
    )
    completed = run_gate(root)
    assert completed.returncode != 0, completed.stdout
    assert where in completed.stdout
    assert "cli/root.py" not in completed.stdout


def test_the_step_passes_when_only_the_roots_name_it(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {
            "cli/backup.py": "if request.dry_run:\n    pass\n",
            "api/x.py": "what_if = True\n",
            "core/y.py": "outcome = 'skipped_dry_run'\n",
        },
    )
    completed = run_gate(root)
    assert completed.returncode == 0, completed.stdout


def test_the_repository_passes_the_step() -> None:
    completed = run_gate(PROJECT_ROOT)
    assert completed.returncode == 0, completed.stdout
