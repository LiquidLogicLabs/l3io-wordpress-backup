"""`make release` refuses unless preflight passed for HEAD and GitHub CI is green.

Family rule (Shawn, 2026-11-08). Each refusal path is proved by planting the
failure it exists to catch, against a scratch git repository carrying THIS repo's
real workflow files -- so the set of workflows the gate requires is the one the
gate derives from them, not a list written here. `gh` is replaced by a stub that
prints a planted `gh run list` answer.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
GATE = ROOT / "scripts" / "release-gate.py"


def _git(repo: pathlib.Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603 - fixed git arguments in a scratch repo
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],  # noqa: S607
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _workflow_name(path: pathlib.Path) -> str:
    return str(yaml.safe_load(path.read_text(encoding="utf-8"))["name"])


CI_NAME = _workflow_name(ROOT / ".github/workflows/ci.yml")


@pytest.fixture
def repo(tmp_path: pathlib.Path) -> pathlib.Path:
    work = tmp_path / "repo"
    shutil.copytree(ROOT / ".github" / "workflows", work / ".github" / "workflows")
    _git(work, "init", "-q")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "scratch")
    return work


def _stamp(repo: pathlib.Path, value: str) -> None:
    path = pathlib.Path(_git(repo, "rev-parse", "--git-path", "l3io-preflight"))
    (path if path.is_absolute() else repo / path).write_text(value + "\n")


def _required(repo: pathlib.Path) -> set[str]:
    sys.path.insert(0, str(GATE.parent))
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location("release_gate", GATE)
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return set(module.required_workflows(repo, "main"))
    finally:
        sys.path.remove(str(GATE.parent))


def _green(repo: pathlib.Path) -> list[dict[str, str]]:
    return [
        {"workflowName": name, "status": "completed", "conclusion": "success"}
        for name in sorted(_required(repo))
    ]


def _run(
    repo: pathlib.Path, tmp_path: pathlib.Path, runs: object, *, gh_rc: int = 0
) -> subprocess.CompletedProcess[str]:
    answer = tmp_path / "gh-answer.json"
    answer.write_text(json.dumps(runs))
    stub = tmp_path / "gh"
    stub.write_text(f'#!/bin/sh\ncat "{answer}"\nexit {gh_rc}\n')
    stub.chmod(0o755)
    return subprocess.run(  # noqa: S603 - the gate under test, by absolute path
        [sys.executable, str(GATE)],
        cwd=repo,
        env={"PATH": "/usr/bin:/bin", "GH": str(stub), "RELEASE_BRANCH": "main"},
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_gate_requires_ci_on_a_push_to_main(repo: pathlib.Path) -> None:
    """Scope assertion: without CI in the required set, a run list holding only
    the faster audit would read as green."""
    assert CI_NAME in _required(repo)


def test_it_passes_with_preflight_recorded_and_every_run_green(
    repo: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    _stamp(repo, _git(repo, "rev-parse", "HEAD"))
    done = _run(repo, tmp_path, _green(repo))
    assert done.returncode == 0, done.stdout + done.stderr


def test_no_preflight_record_is_refused(
    repo: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    done = _run(repo, tmp_path, _green(repo))
    assert done.returncode == 1
    assert "preflight" in done.stdout


def test_a_preflight_record_for_another_commit_is_refused(
    repo: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    _stamp(repo, _git(repo, "rev-parse", "HEAD"))
    (repo / "new").write_text("x")
    _git(repo, "add", "new")
    _git(repo, "commit", "-q", "-m", "after preflight")
    done = _run(repo, tmp_path, _green(repo))
    assert done.returncode == 1
    assert "preflight" in done.stdout


def test_no_github_runs_is_refused(repo: pathlib.Path, tmp_path: pathlib.Path) -> None:
    _stamp(repo, _git(repo, "rev-parse", "HEAD"))
    done = _run(repo, tmp_path, [])
    assert done.returncode == 1
    assert "no runs" in done.stdout


@pytest.mark.parametrize(
    ("status", "conclusion"),
    [
        ("completed", "failure"),
        ("completed", "cancelled"),
        ("in_progress", ""),
        ("completed", "skipped"),
    ],
)
def test_a_run_that_is_not_green_is_refused(
    repo: pathlib.Path, tmp_path: pathlib.Path, status: str, conclusion: str
) -> None:
    _stamp(repo, _git(repo, "rev-parse", "HEAD"))
    runs = _green(repo)
    runs[0] = {**runs[0], "status": status, "conclusion": conclusion}
    done = _run(repo, tmp_path, runs)
    assert done.returncode == 1
    assert "not green" in done.stdout


def test_a_required_workflow_with_no_run_is_refused(
    repo: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """Attacks the scope: every listed run is green, but CI never ran."""
    _stamp(repo, _git(repo, "rev-parse", "HEAD"))
    runs = [r for r in _green(repo) if r["workflowName"] != CI_NAME]
    done = _run(repo, tmp_path, runs)
    assert done.returncode == 1
    assert CI_NAME in done.stdout


def test_gh_failing_is_refused(repo: pathlib.Path, tmp_path: pathlib.Path) -> None:
    _stamp(repo, _git(repo, "rev-parse", "HEAD"))
    done = _run(repo, tmp_path, _green(repo), gh_rc=1)
    assert done.returncode == 1


def _recipe(target: str) -> str:
    """The target's recipe text, read from the Makefile. Not `make -n`: GNU make
    still EXECUTES a recipe line that contains $(MAKE), which here is `make ci`."""
    lines = (ROOT / "Makefile").read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{target}:"))
    body = []
    for line in lines[start + 1 :]:
        if not line.startswith("\t"):
            break
        body.append(line)
    assert body, f"no recipe for {target}"
    return "\n".join(body)


def test_make_release_runs_the_gate_before_it_dispatches() -> None:
    recipe = _recipe("release")
    assert "scripts/release-gate.py" in recipe
    assert recipe.index("scripts/release-gate.py") < recipe.index("workflow run")


def test_make_preflight_records_only_after_make_ci() -> None:
    recipe = _recipe("preflight")
    removed = recipe.index("rm -f")
    ci = recipe.index("--no-print-directory ci")
    written = recipe.rindex("> ")
    assert removed < ci < written
