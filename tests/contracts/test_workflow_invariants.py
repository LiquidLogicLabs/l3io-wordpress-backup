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
from collections.abc import Callable, Iterator
from types import ModuleType

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


#: ``uv run ... --with <the built wheel>`` — a step that runs the installed wheel.
_RUNS_THE_WHEEL = re.compile(r'--with\s+"\$(?:wheel|WHEEL)"')


def _wheel_jobs() -> dict[str, list[str]]:
    """Every job with a step that runs the built wheel, mapped to those steps."""
    jobs: dict[str, list[str]] = {}
    for wf in WORKFLOWS:
        for where, code, _step in _steps(wf):
            if _RUNS_THE_WHEEL.search(code):
                jobs.setdefault(where, []).append(code)
    return jobs


def test_python_dash_m_wpbackup2_runs_wherever_the_console_script_does() -> None:
    """AD-22 freezes both invocations, so the wheel is proved to answer both.

    Scope is every job that runs the console script ``wpbackup2 --version`` from
    the built wheel, found by walking the workflows. The module run must ``cd``
    away from the checkout first: from the repository root a ``wpbackup2/`` tree
    on disk would answer instead of the wheel and pass for the wrong reason.
    """
    console = {
        where: steps
        for where, steps in _wheel_jobs().items()
        if any(re.search(r'"\s+wpbackup2 --version', code) for code in steps)
    }
    assert console, "no job runs the console script from the built wheel"
    for where, steps in console.items():
        module = [c for c in steps if "python -m wpbackup2 --version" in c]
        assert module, f"{where}: runs `wpbackup2` but never `python -m wpbackup2`"
        assert all(re.search(r"^\s*cd\s", c, re.MULTILINE) for c in module), (
            f"{where}: `python -m wpbackup2` runs inside the checkout"
        )


def _recursive_grep_steps() -> list[tuple[str, str]]:
    """Every workflow step whose script scans a tree with ``grep -r``."""
    found: list[tuple[str, str]] = []
    for wf in WORKFLOWS:
        for job_name, job in _jobs(wf).items():
            for step in job.get("steps") or []:
                script = step.get("run") or ""
                if re.search(r"\bgrep\s+-[a-zA-Z]*r", script):
                    found.append((f"{wf.name}:{job_name}:{step.get('name')}", script))
    return found


GREP_STEPS = _recursive_grep_steps()


def test_there_are_grep_steps_to_check() -> None:
    assert GREP_STEPS, "no grep -r step found; the test below would pass vacuously"


@pytest.mark.parametrize(
    ("label", "script"), GREP_STEPS, ids=[label for label, _ in GREP_STEPS]
)
def test_a_grep_gate_fails_when_its_tree_is_missing(
    label: str, script: str, tmp_path: pathlib.Path
) -> None:
    """AD-12 (BL-E002-096): run where the scanned tree does not exist (a
    package rename), the step must fail, never pass over nothing; run over
    this repo, it passes. GitHub's default ``bash -e`` shell."""
    import shutil
    import subprocess

    bash = shutil.which("bash")
    assert bash is not None
    root = pathlib.Path(__file__).resolve().parents[2]

    def run(cwd: pathlib.Path) -> int:
        return subprocess.run(  # noqa: S603 - a fixed shell over a workflow script
            [bash, "-e", "-c", script], cwd=cwd, capture_output=True, check=False
        ).returncode

    assert run(tmp_path) != 0, f"{label} passes over a missing tree"
    assert run(root) == 0, f"{label} fails over this repo"


# ---- act runs every leg it can (2026-11-08) ---------------------------------
#
# The `test` job's matrix used to collapse to py3.11 x mysql80 under act, so no
# local run ever started MariaDB, and the mariadb legs went red on GitHub with
# `make ci` green. The rule -- collapse an axis under act only where its legs
# would share a host port -- lives in scripts/check-cicd-alignment.py, which
# sweeps the whole package family. These tests apply that same function to every
# workflow here, so the rule is enforced on each change and not only when the
# family sweep is run by hand.


def _alignment() -> ModuleType:
    import importlib.util

    root = pathlib.Path(__file__).resolve().parents[2]
    path = root / "scripts/check-cicd-alignment.py"
    spec = importlib.util.spec_from_file_location("check_cicd_alignment", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _act_matrix_findings(doc: dict) -> list[str]:
    findings: list[str] = []
    _alignment().check_act_matrix(doc, lambda _area, msg: findings.append(msg))
    return findings


def _load(path: pathlib.Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("wf", WORKFLOWS, ids=lambda p: p.name)
def test_act_collapses_an_axis_only_where_legs_share_a_host_port(
    wf: pathlib.Path,
) -> None:
    assert _act_matrix_findings(_load(wf)) == []


def _service_jobs() -> list[tuple[str, str]]:
    return [
        (wf.name, name)
        for wf in WORKFLOWS
        for name, job in _jobs(wf).items()
        if job.get("services") and (job.get("strategy") or {}).get("matrix")
    ]


SERVICE_JOBS = _service_jobs()


def test_there_is_a_service_matrix_to_check() -> None:
    assert SERVICE_JOBS, "no matrix job with services; the tests below pass vacuously"


@pytest.mark.parametrize(("wf", "job"), SERVICE_JOBS, ids=lambda v: str(v))
def test_every_service_matrix_runs_in_full_under_act(wf: str, job: str) -> None:
    """Stricter than the family rule, which still PERMITS a collapse where legs
    share a host port: here every service matrix must be isolated under act, so
    the collapsed py3.11 x mysql80 layout cannot come back. Isolation plus the
    rule above (no collapse when isolated) means act runs every leg."""
    doc = _load(pathlib.Path(__file__).resolve().parents[2] / ".github/workflows" / wf)
    isolated = _alignment().services_isolated_under_act(doc["jobs"][job])
    assert isolated, (
        f"{wf}:{job} is not isolated under act -- give it a LOCAL_ACT-only "
        "`container:` and publish no fixed host port under act (see ci.yml `test`)"
    )


def _collapse(job: dict) -> None:
    axis = next(iter(job["strategy"]["matrix"]))
    job["strategy"]["matrix"][axis] = (
        "${{ vars.LOCAL_ACT && fromJSON('[]') || fromJSON('[]') }}"
    )


def _host_port(job: dict) -> None:
    for svc in job["services"].values():
        svc["ports"] = ["3306:3306"]


def _container_on_github(job: dict) -> None:
    job["container"] = "${{ vars.LOCAL_ACT && 'img' || 'img' }}"


def _no_container(job: dict) -> None:
    job.pop("container", None)


@pytest.mark.parametrize(
    "plant",
    [_collapse, _host_port, _container_on_github, _no_container],
    ids=["collapsed-axis", "host-port", "container-on-github", "no-container"],
)
@pytest.mark.parametrize(("wf", "job"), SERVICE_JOBS, ids=lambda v: str(v))
def test_the_act_matrix_rule_rejects_a_planted_violation(
    wf: str, job: str, plant: Callable[[dict], None]
) -> None:
    """Each plant attacks one condition of isolation (or the collapse itself);
    every one must produce a finding, or the rule is checking less than it says."""
    doc = _load(pathlib.Path(__file__).resolve().parents[2] / ".github/workflows" / wf)
    plant(doc["jobs"][job])
    assert _act_matrix_findings(doc), f"{wf}:{job} -- planted violation not reported"


def _narrowed_matrices(doc: dict) -> list[str]:
    """Jobs whose matrix (any axis, include or exclude) depends on LOCAL_ACT."""
    return [
        name
        for name, job in (doc.get("jobs") or {}).items()
        if "LOCAL_ACT" in str((job.get("strategy") or {}).get("matrix") or "")
    ]


@pytest.mark.parametrize("wf", WORKFLOWS, ids=lambda p: p.name)
def test_no_matrix_is_narrowed_under_act(wf: pathlib.Path) -> None:
    """Family rule, as in wordpress-database: the full matrix runs locally."""
    assert _narrowed_matrices(_load(wf)) == []


def test_a_narrowed_matrix_is_reported() -> None:
    doc = _load(
        pathlib.Path(__file__).resolve().parents[2] / ".github/workflows/ci.yml"
    )
    _collapse(doc["jobs"]["test"])
    assert _narrowed_matrices(doc) == ["test"]


# ---- the suite is split by tier, exactly (2026-11-09) ------------------------
#
# The database legs run `-m integration`, `unit` runs `-m "not integration"`, so
# the twelve database legs no longer repeat the whole suite. The split is only
# safe while it is exact; scripts/check-cicd-alignment.py's check_test_tiers
# states what that means, and these tests apply it here. That a test which needs
# a database carries the marker is tests/contracts/test_integration_marker_reach.py.

CI_YML = pathlib.Path(__file__).resolve().parents[2] / ".github/workflows/ci.yml"


def _tier_findings(doc: dict) -> list[str]:
    """The family rule, plus this repo's decision that the split exists."""
    findings: list[str] = []
    module = _alignment()
    module.check_test_tiers(doc, lambda _area, msg: findings.append(msg))
    if not module.tier_runs(doc):
        findings.append("ci.yml does not split its suite by the integration marker")
    return findings


@pytest.mark.parametrize("wf", WORKFLOWS, ids=lambda p: p.name)
def test_every_tier_split_is_exact(wf: pathlib.Path) -> None:
    findings: list[str] = []
    _alignment().check_test_tiers(_load(wf), lambda _a, msg: findings.append(msg))
    assert findings == []


def test_ci_splits_its_suite_into_two_complementary_tiers() -> None:
    doc = _load(CI_YML)
    assert _tier_findings(doc) == []
    tiers = _alignment().tier_runs(doc)
    service_jobs = {n for n, j in doc["jobs"].items() if j.get("services")}
    assert set(tiers) & service_jobs, "no database job runs a tier"
    assert set(tiers) - service_jobs, "no no-database job runs a tier"


def _tier_step(job: dict) -> dict:
    """The job's one whole-suite pytest step, found by the rule's own parser."""
    module = _alignment()
    steps = [
        st for st in job["steps"] if module.tier_runs({"jobs": {"j": {"steps": [st]}}})
    ]
    assert len(steps) == 1, steps
    return steps[0]


def _rewrite(job_name: str, old: str, new: str) -> Callable[[dict], None]:
    def plant(doc: dict) -> None:
        step = _tier_step(doc["jobs"][job_name])
        assert old in step["run"], (old, step["run"])
        step["run"] = step["run"].replace(old, new)

    return plant


def _narrow_python(job_name: str) -> Callable[[dict], None]:
    def plant(doc: dict) -> None:
        doc["jobs"][job_name]["strategy"]["matrix"]["python"].pop()

    return plant


def _db_job(doc: dict) -> str:
    return next(n for n, j in doc["jobs"].items() if j.get("services"))


@pytest.mark.parametrize(
    "plant",
    [
        # A database leg running the whole suite again.
        lambda d: _rewrite(_db_job(d), "-m integration ", "")(d),
        # The no-database tier dropping the complement: it runs integration tests
        # with no database, which skip.
        _rewrite("unit", '-m "not integration" ', ""),
        # Both dropped: the old layout, every database leg the whole suite.
        lambda d: (
            _rewrite(_db_job(d), "-m integration ", "")(d),
            _rewrite("unit", '-m "not integration" ', "")(d),
        ),
        # A marker expression that is no longer the complement.
        _rewrite("unit", '"not integration"', '"not integration and not slow"'),
        lambda d: _rewrite(_db_job(d), "-m integration", '-m "integration or slow"')(d),
        # A tier narrowed further.
        _rewrite("unit", '-m "not integration"', '-m "not integration" -k "not tar"'),
        lambda d: _rewrite(_db_job(d), "-m integration", "-m integration --lf")(d),
        # A tier narrowed to a path: still a tier run, so still checked.
        _rewrite("unit", '-m "not integration"', '-m "not integration" tests/unit'),
        # The tiers on different interpreters.
        _narrow_python("unit"),
        lambda d: _narrow_python(_db_job(d))(d),
        # A tier run moved into a script the rule cannot read is NOT caught by the
        # family rule; this repo's existence check catches the missing split.
        lambda d: (
            _tier_step(d["jobs"][_db_job(d)]).update(run="make test-db"),
            _tier_step(d["jobs"]["unit"]).update(run="make test-unit"),
        ),
    ],
    ids=[
        "db-full-suite",
        "unit-no-complement",
        "both-full-suite",
        "unit-not-complement",
        "db-not-complement",
        "unit-narrowed",
        "db-narrowed",
        "unit-narrowed-to-path",
        "unit-fewer-pythons",
        "db-fewer-pythons",
        "moved-to-make",
    ],
)
def test_the_tier_rule_rejects_a_planted_violation(
    plant: Callable[[dict], object],
) -> None:
    doc = _load(CI_YML)
    plant(doc)
    assert _tier_findings(doc), "planted violation not reported"


def test_passing_tests_leave_no_temporary_files() -> None:
    """act gives a job container one minute to be removed (act v0.2.89,
    pkg/runner/job_executor.go), and the removal time grows with what the job
    wrote. Every passing test's tmp_path kept was several GB per leg, and four
    legs ending together aborted `make ci` with "context deadline exceeded"
    (2026-11-09). pytest's own setting deletes them; this keeps it set."""
    import tomllib

    root = pathlib.Path(__file__).resolve().parents[2]
    ini = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    options = ini["tool"]["pytest"]["ini_options"]
    assert options.get("tmp_path_retention_policy") == "failed"
