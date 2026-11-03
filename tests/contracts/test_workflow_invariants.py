"""Invariants over the workflow files themselves, derived from the files on disk.

Each of these was a real defect in this repo, found by a sibling session reporting
it in theirs. They are tests rather than prose because a rule that lives only in a
comment gets violated by the next person to add a job -- and the whole point of
these two is that the violation is *silent*.

The scope is discovered by walking ``.github/workflows/``. It is never a list of
job names: a hand-kept roster is how a guard ends up reporting success over a set
that no longer contains what matters.
"""

from __future__ import annotations

import pathlib
import re
from collections.abc import Iterator

import pytest
import yaml

WORKFLOWS = sorted(
    (pathlib.Path(__file__).resolve().parents[2] / ".github/workflows").glob("*.yml")
)


def _jobs(path: pathlib.Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")).get("jobs") or {}


def test_there_are_workflows_to_check() -> None:
    """Scope assertion. Without it every test below passes over an empty set."""
    assert WORKFLOWS, "no workflow files found; the tests below would pass vacuously"


@pytest.mark.parametrize("wf", WORKFLOWS, ids=lambda p: p.name)
def test_every_checkout_fetches_full_history(wf: pathlib.Path) -> None:
    """``actions/checkout`` defaults to depth 1 and fetches no tags.

    The version is read from git, so a shallow checkout makes setuptools-scm find
    no tags and fall back to ``0.1.dev1`` -- and the job passes GREEN with a
    misversioned artifact. There is no error anywhere, which is what makes it
    worth a test.

    Applied to every checkout rather than only the jobs that build, because a
    dynamic version means ``uv run`` builds the package: all three of this repo's
    lint-only jobs build one. Which jobs build is a fact that changes.
    """
    offenders = []
    for name, job in _jobs(wf).items():
        for i, step in enumerate(job.get("steps") or []):
            if "actions/checkout" in str(step.get("uses", "")):
                depth = (step.get("with") or {}).get("fetch-depth")
                if str(depth) != "0":
                    offenders.append(f"{name}[step {i}] fetch-depth={depth!r}")
    assert not offenders, (
        f"{wf.name}: checkout without fetch-depth: 0 -> "
        f"a shallow clone silently versions the build 0.1.dev1: {offenders}"
    )


@pytest.mark.parametrize("wf", WORKFLOWS, ids=lambda p: p.name)
def test_no_unguarded_wheel_glob(wf: pathlib.Path) -> None:
    """``ls dist/*.whl`` assumes a set of size one and never checks it.

    With two wheels it expands to two paths and the consumer gets a nonsense
    argument; with none the glob does not expand and the literal ``dist/*.whl`` is
    passed. Neither fails where the mistake is. Comments mentioning the pattern are
    excluded, or this test would flag the comment explaining why it is banned.
    """
    offenders = []
    for name, job in _jobs(wf).items():
        for i, step in enumerate(job.get("steps") or []):
            run = str(step.get("run", ""))
            code = "\n".join(
                line for line in run.splitlines() if not line.lstrip().startswith("#")
            )
            if "ls dist/*.whl" in code:
                offenders.append(f"{name}[step {i}]")
    assert not offenders, (
        f"{wf.name}: unguarded `ls dist/*.whl` -> resolve it once and assert the "
        f"set has exactly one member: {offenders}"
    )


# release.yml moves floating `v<major>` / `v<major>.<minor>` tags onto every
# release commit. A bare `v*` push filter starts a publish on each of them.
FLOATING = ["v1", "v1.0", "v12", "v12.3"]
RELEASES = ["v1.0.0", "v1.2.3", "v12.0.10", "v1.0.0rc1"]


def _starts_a_run(tag: str, patterns: list[str]) -> bool:
    """GitHub's tag filter: the last matching pattern wins, ``!`` negates."""
    from fnmatch import fnmatchcase

    hit = False
    for pattern in patterns:
        if fnmatchcase(tag, pattern.removeprefix("!")):
            hit = not pattern.startswith("!")
    return hit


def _tag_patterns(wf: pathlib.Path) -> list[str]:
    doc = yaml.safe_load(wf.read_text(encoding="utf-8"))
    # PyYAML parses a bare `on:` key as boolean True.
    on = doc.get(True, doc.get("on")) or {}
    push = (on.get("push") or {}) if isinstance(on, dict) else {}
    return list(push.get("tags") or [])


@pytest.mark.parametrize("wf", WORKFLOWS, ids=lambda p: p.name)
def test_no_floating_tag_starts_a_workflow(wf: pathlib.Path) -> None:
    patterns = _tag_patterns(wf)
    started = [t for t in FLOATING if _starts_a_run(t, patterns)]
    assert not started, f"{wf.name} runs on floating tag(s) {started}: {patterns}"


def test_a_release_tag_still_starts_publish() -> None:
    patterns = _tag_patterns(WORKFLOWS[0].parent / "publish.yml")
    assert patterns, "publish.yml has no tag trigger -- the check above is vacuous"
    assert all(_starts_a_run(t, patterns) for t in RELEASES), patterns


def test_release_notes_never_start_from_a_floating_tag() -> None:
    """Config's v0.1.0 shipped "no changes": the builder took the floating v0.1.

    The notes builder must be told its lower bound, and that bound must come from
    a step that matches only three-component tags.
    """
    jobs = _jobs(WORKFLOWS[0].parent / "publish.yml")
    steps = (jobs.get("release") or {}).get("steps") or []
    assert steps, "publish.yml has no release job -- the check below is vacuous"
    builder = next(
        s
        for s in steps
        if "git-action-release-changelog-builder" in str(s.get("uses", ""))
    )
    from_tag = str((builder.get("with") or {}).get("from-tag", ""))
    assert "steps.previous.outputs.tag" in from_tag, f"from-tag is {from_tag!r}"
    previous = next((s for s in steps if s.get("id") == "previous"), None)
    assert previous is not None, "no step with id 'previous' computes the lower bound"
    assert "--match 'v[0-9]*.[0-9]*.[0-9]*'" in str(previous.get("run", ""))
    assert steps.index(previous) < steps.index(builder)


def _compute_next_version(
    tmp_path: pathlib.Path, tags: list[str], env: dict[str, str]
) -> tuple[int, str]:
    """Run release.yml's real "compute the next version" script in a scratch repo."""
    import os
    import subprocess

    jobs = _jobs(WORKFLOWS[0].parent / "release.yml")
    step = next(
        s
        for job in jobs.values()
        for s in job.get("steps") or []
        if s.get("id") == "next"
    )
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)  # noqa: S603
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "c"], check=True)  # noqa: S603
    for tag in tags:
        subprocess.run([*git, "tag", tag], check=True)  # noqa: S603
    out = tmp_path / "out"
    out.touch()
    full_env = {
        "PATH": os.environ["PATH"],
        "GITHUB_OUTPUT": str(out),
        "BUMP": "patch",
        **env,
    }
    proc = subprocess.run(  # noqa: S603
        ["bash", "-c", str(step["run"])],  # noqa: S607
        cwd=repo,
        env=full_env,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, out.read_text() + proc.stdout + proc.stderr


def test_a_failed_tag_lookup_is_a_first_release_only_when_no_release_tag_exists(
    tmp_path: pathlib.Path,
) -> None:
    """The lookup step tolerates the action erroring when no v*.*.* tag exists.

    A failure for any other reason -- network, auth, a broken action -- must not
    be read as "first release": with v0.0.1 already tagged that would compute
    v0.0.1 again.
    """
    failed = {"LATEST_OK": "failure", "HAS_TAG": "", "IS_VALID": ""}
    code, out = _compute_next_version(
        tmp_path / "first", ["baseline-pre-rewrite"], failed
    )
    assert code == 0, out
    assert "version=0.0.1" in out

    code, out = _compute_next_version(
        tmp_path / "later", ["baseline-pre-rewrite", "v0.0.1"], failed
    )
    assert code != 0, f"a failed lookup with v0.0.1 tagged must stop the release: {out}"
    assert "version=" not in out


def _wp_cli_installs() -> list[tuple[str, str, dict]]:
    found = []
    for wf in WORKFLOWS:
        for name, job in _jobs(wf).items():
            for step in job.get("steps") or []:
                run = str(step.get("run", ""))
                code = "\n".join(
                    line
                    for line in run.splitlines()
                    if not line.lstrip().startswith("#")
                )
                if "wp-cli" in code:
                    found.append((f"{wf.name}:{name}", code, step))
    return found


def test_wp_cli_is_installed_somewhere() -> None:
    """Scope assertion: the pin test below must have a step to examine."""
    assert _wp_cli_installs(), "no workflow step installs WP-CLI"


def test_wp_cli_is_pinned_and_checksummed() -> None:
    """A moving "latest" phar made the WP-CLI under test change between runs.

    E007-S01-004 makes the CI version the supported minimum, so it must be one
    release, verified against a checksum written into the workflow.
    """
    for where, run, step in _wp_cli_installs():
        env = step.get("env") or {}
        assert "gh-pages" not in run, f"{where}: installs the moving gh-pages phar"
        assert re.fullmatch(r"\d+\.\d+\.\d+", str(env.get("WP_CLI_VERSION", ""))), (
            f"{where}: WP_CLI_VERSION is not a pinned x.y.z release"
        )
        assert re.fullmatch(r"[0-9a-f]{128}", str(env.get("WP_CLI_SHA512", ""))), (
            f"{where}: WP_CLI_SHA512 is not a hardcoded sha512"
        )
        assert "sha512sum -c" in run, f"{where}: the download is never verified"


def _steps(wf: pathlib.Path) -> Iterator[tuple[str, str, dict]]:
    """Every step of every job, with its ``run:`` stripped of comment lines."""
    for job_id, job in _jobs(wf).items():
        for step in job.get("steps") or []:
            lines = (step.get("run") or "").splitlines()
            code = "\n".join(ln for ln in lines if not ln.lstrip().startswith("#"))
            yield f"{wf.name}:{job_id}", code, step


def _extras_installs() -> list[tuple[str, str, dict]]:
    """Every step that installs all of this package's extras."""
    return [
        found
        for wf in WORKFLOWS
        for found in _steps(wf)
        if "--all-extras" in found[1] and "uv pip install" in found[1]
    ]


def test_every_extra_is_install_checked_wheels_only() -> None:
    """FR-25: every runtime dependency resolves from its declared index.

    The base-wheel checks cannot see a broken extra. E004 gate AG-3 (2026-10-28)
    found the age passphrase binder resolvable only from a git source, so an
    operator's ``[database]`` install would have built Rust from source. Each
    extras check installs EVERY extra (``--all-extras``, so a new extra is
    covered without an edit) with ``--no-build``.
    """
    installs = _extras_installs()
    assert installs, "no workflow step installs the package's extras"
    for where, code, _step in installs:
        assert "--no-build" in code, f"{where}: a source build would pass this check"
        assert "-r pyproject.toml" in code, f"{where}: not reading pyproject.toml"


def test_dev_and_test_use_the_sources_a_pypi_release_does_not() -> None:
    """Which index, by uv's built-in switch rather than scripting (2026-11-03).

    Development and test honour ``[tool.uv.sources]`` (the family from TestPyPI);
    a PyPI release passes ``--no-sources`` and so resolves from PyPI alone. Both
    halves must exist: ci.yml checks the development indexes on every run, and
    publish.yml checks PyPI only when the target is PyPI. Losing the second
    would let a PyPI release ship extras that cannot install from PyPI.
    """
    installs = _extras_installs()
    dev = [i for i in installs if i[0].startswith("ci.yml:")]
    release = [i for i in installs if i[0].startswith("publish.yml:")]
    assert dev, "ci.yml has no extras install check"
    assert release, "publish.yml has no extras install check for a PyPI release"
    for where, code, _step in dev:
        assert "--no-sources" not in code, (
            f"{where}: development must honour [tool.uv.sources] (TestPyPI)"
        )
    for where, code, step in release:
        assert "--no-sources" in code, f"{where}: a PyPI release must ignore sources"
        cond = str(step.get("if", ""))
        assert "inputs.target == 'pypi'" in cond, (
            f"{where}: the PyPI-only check must run exactly for a PyPI release, "
            f"got if: {cond!r}"
        )


def test_a_login_gated_index_never_runs_on_pull_requests() -> None:
    """A step that uses the login-gated family index must not run on pull requests.

    The family Gitea index needs a login and this repository is public, so a
    fork's pull request has no ``GITEA_TOKEN``. Today no install step uses that
    index (2026-11-02). This guards the way back: such a step must run off pull
    requests, gated on ``github.event_name`` and never on whether a secret is
    empty, which evaluates silently (publish.yml records that trap).
    """
    for wf in WORKFLOWS:
        for where, code, step in _steps(wf):
            if "--index family=" not in code:
                continue
            cond = str(step.get("if", ""))
            assert "secrets." not in cond, (
                f"{where}: gated on a secret, which evaluates empty and skips silently"
            )
            assert cond.strip() == "github.event_name != 'pull_request'", (
                f"{where}: a login-gated step must run off pull requests, got {cond!r}"
            )
