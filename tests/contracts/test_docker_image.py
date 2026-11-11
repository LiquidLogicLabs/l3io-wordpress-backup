"""The official image's supply-chain and release rules (owner decision 77).

Each rule is checked over a scope DERIVED from the files: every Dockerfile and
compose file in the tree, every `uses:` in docker.yml, the registries named in
docker.yml's own `resolve` step. Each rule also has a planted violation, aimed
at the rule and at its scope, that it must report.

What is NOT checked here: that a pinned digest or SHA still exists upstream
(scripts/check-cicd-alignment.py resolves action SHAs online; a missing image
digest fails the build), and anything about the built image (tests/sidecar/run.sh).
"""

from __future__ import annotations

import copy
import json
import pathlib
import re

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "docker" / "Dockerfile"
DOCKER_WF = ROOT / ".github" / "workflows" / "docker.yml"
PUBLISH_WF = ROOT / ".github" / "workflows" / "publish.yml"
LEGS = ROOT / ".github" / "act-dryrun-legs.json"
ENTRYPOINT = (
    ROOT / "docker" / "root" / "usr" / "local" / "bin" / "l3io-wpbackup-entrypoint"
)
SKIP_PARTS = {".git", ".venv", "node_modules", "legacy", "_bmad", "_bmad-output"}

SHA_PIN = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")


def _tree(*patterns: str) -> list[pathlib.Path]:
    found = {
        p
        for pattern in patterns
        for p in ROOT.glob(pattern)
        if p.is_file() and not SKIP_PARTS.intersection(p.relative_to(ROOT).parts)
    }
    return sorted(found)


def _load(path: pathlib.Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# ---- base images pinned by digest -------------------------------------------


def _instructions(text: str) -> list[str]:
    """Dockerfile instructions with continuations joined and comments dropped."""
    out: list[str] = []
    cur = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#") or not line:
            continue
        cur = f"{cur} {line[:-1]}" if line.endswith("\\") else f"{cur} {line}"
        if not line.endswith("\\"):
            out.append(cur.strip())
            cur = ""
    return out


def dockerfile_findings(text: str) -> list[str]:
    """Every FROM is a digest-pinned image (or scratch, or an earlier stage);
    every ADD of a URL carries --checksum=sha256."""
    findings: list[str] = []
    args: dict[str, str] = {}
    stages: set[str] = set()
    for ins in _instructions(text):
        word, _, rest = ins.partition(" ")
        word = word.upper()
        if word == "ARG":
            name, _, default = rest.partition("=")
            if default:
                args[name.strip()] = default.strip()
        elif word == "FROM":
            parts = [p for p in rest.split() if not p.startswith("--")]
            ref = re.sub(
                r"\$\{(\w+)\}|\$(\w+)", lambda m: args.get(m[1] or m[2], m[0]), parts[0]
            )
            if len(parts) >= 3 and parts[1].upper() == "AS":
                stages.add(parts[2])
            if ref == "scratch" or ref in stages:
                continue
            if not DIGEST.search(ref):
                findings.append(f"FROM {parts[0]} -> {ref} is not pinned by digest")
        elif word == "ADD" and re.search(r"\bhttps?://", rest):
            if not re.search(r"--checksum=sha256:[0-9a-f]{64}\b", rest):
                findings.append(f"ADD of a URL without --checksum=sha256: {rest[:80]}")
    return findings


def test_there_are_dockerfiles_to_check() -> None:
    names = {p.relative_to(ROOT).as_posix() for p in _tree("**/Dockerfile*")}
    assert "docker/Dockerfile" in names


@pytest.mark.parametrize(
    "path", _tree("**/Dockerfile*"), ids=lambda p: p.relative_to(ROOT).as_posix()
)
def test_every_base_image_is_digest_pinned_and_every_download_checksummed(
    path: pathlib.Path,
) -> None:
    assert dockerfile_findings(path.read_text(encoding="utf-8")) == []


DOCKERFILE_PLANTS = {
    "a tag-only base": "FROM ubuntu:26.04\n",
    "a tag-only base through an ARG": "ARG B=ubuntu:26.04\nFROM ${B}\n",
    "an ARG default overridden to a tag": (
        "ARG GO_IMAGE=golang:1.27\nFROM ${GO_IMAGE} AS g\n"
    ),
    "a download without a checksum": "ADD https://example.invalid/x.tar.gz /x\n",
}


@pytest.mark.parametrize(
    "plant", DOCKERFILE_PLANTS.values(), ids=DOCKERFILE_PLANTS.keys()
)
def test_a_planted_dockerfile_violation_is_reported(plant: str) -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert dockerfile_findings(text) == [], "the real Dockerfile must be clean first"
    assert dockerfile_findings(text + plant), "the planted violation was not reported"


def compose_findings(doc: dict) -> list[str]:
    return [
        f"{name}: image {svc['image']} is not pinned by digest"
        for name, svc in (doc.get("services") or {}).items()
        if "image" in svc
        and not str(svc["image"]).startswith("${")
        and not DIGEST.search(str(svc["image"]))
    ]


@pytest.mark.parametrize(
    "path", _tree("**/compose*.y*ml"), ids=lambda p: p.relative_to(ROOT).as_posix()
)
def test_every_compose_image_is_digest_pinned(path: pathlib.Path) -> None:
    assert compose_findings(_load(path)) == []


def test_a_planted_compose_violation_is_reported() -> None:
    doc = _load(ROOT / "tests" / "sidecar" / "compose.yaml")
    assert compose_findings(doc) == []
    doc["services"]["db-mysql"]["image"] = "mysql:8.0"
    assert compose_findings(doc)


# ---- the image runs as uid/gid 33 by default ----------------------------------


def uid_findings(dockerfile: str, entrypoint: str) -> list[str]:
    findings = []
    env = " ".join(i for i in _instructions(dockerfile) if i.upper().startswith("ENV "))
    for var in ("PUID", "PGID"):
        if not re.search(rf"\b{var}=33\b", env):
            findings.append(f"the Dockerfile does not set ENV {var}=33")
        if f"${{{var}:-33}}" not in entrypoint:
            findings.append(f"the entrypoint's {var} default is not 33")
    if "chown 33:33" not in dockerfile:
        findings.append("the image's writable directories are not built owned by 33:33")
    return findings


def test_the_image_runs_as_33_by_default() -> None:
    assert (
        uid_findings(
            DOCKERFILE.read_text(encoding="utf-8"),
            ENTRYPOINT.read_text(encoding="utf-8"),
        )
        == []
    )


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("PUID=33", "PUID=911"),
        ("PGID=33", "PGID=1000"),
        ("chown 33:33", "chown 911:911"),
    ],
)
def test_a_planted_uid_change_is_reported(old: str, new: str) -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert old in text
    findings = uid_findings(
        text.replace(old, new), ENTRYPOINT.read_text(encoding="utf-8")
    )
    assert findings


# ---- docker.yml: pins, triggers, gates ----------------------------------------


def _wf() -> dict:
    return _load(DOCKER_WF)


def _triggers(doc: dict) -> dict:
    return doc.get(True, doc.get("on")) or {}


def _steps(doc: dict) -> list[tuple[str, dict]]:
    return [(j, s) for j, job in doc["jobs"].items() for s in job.get("steps") or []]


def pin_findings(text: str) -> list[str]:
    findings = []
    for m in re.finditer(r"uses:\s*([^\s@]+)@(\S+)(.*)", text):
        action, ref, rest = m.group(1), m.group(2), m.group(3)
        if not SHA_PIN.match(ref):
            findings.append(f"{action}@{ref} is not pinned to a full commit SHA")
        elif not re.search(r"#\s*v\d", rest):
            findings.append(
                f"{action}@{ref} has no `# vX.Y.Z` comment naming its release"
            )
    return findings


def test_every_docker_workflow_action_is_sha_pinned() -> None:
    """Scope: docker.yml, the workflow that holds packages: write, id-token:
    write and the Docker Hub token. The older workflows are tag-pinned by the
    family convention; that gap is recorded in docs/docker.md, not covered here."""
    text = DOCKER_WF.read_text(encoding="utf-8")
    assert re.search(r"uses:", text), "no `uses:` to check"
    assert pin_findings(text) == []


@pytest.mark.parametrize(
    "plant",
    ["      - uses: docker/login-action@v4\n", "      - uses: x/y@" + "a" * 40 + "\n"],
    ids=["a tag", "a SHA without its release comment"],
)
def test_a_planted_unpinned_action_is_reported(plant: str) -> None:
    assert pin_findings(DOCKER_WF.read_text(encoding="utf-8") + plant)


def test_only_a_pypi_publish_or_a_dispatch_starts_the_image_workflow() -> None:
    """No push or pull_request: nothing a contributor starts reaches the
    packages: write token. workflow_run names the Publish workflow by its name,
    read from publish.yml."""
    on = _triggers(_wf())
    assert set(on) == {"workflow_run", "workflow_dispatch"}
    assert on["workflow_run"]["workflows"] == [_load(PUBLISH_WF)["name"]]
    assert on["workflow_run"]["types"] == ["completed"]
    assert on["workflow_dispatch"]["inputs"]["version"]["required"] is True


def gate_findings(doc: dict, publish: dict) -> list[str]:
    """The push is gated on the release being published to PyPI."""
    findings = []
    jobs = doc["jobs"]
    pypi_job = publish["jobs"]["publish-pypi"]["name"]
    resolve = "\n".join(s.get("run", "") for s in jobs["resolve"].get("steps") or [])
    if f'select(.name == "{pypi_job}")' not in resolve:
        findings.append(f"resolve does not check the Publish run's {pypi_job!r} job")
    if (
        "pip download" not in resolve
        or '"l3io-wordpress-backup==$VERSION"' not in resolve
    ):
        findings.append("resolve does not wait for the version to install from PyPI")

    def closure(name: str) -> set[str]:
        seen: set[str] = set()
        todo = [name]
        while todo:
            n = todo.pop()
            needs = jobs[n].get("needs") or []
            for d in [needs] if isinstance(needs, str) else needs:
                if d not in seen:
                    seen.add(d)
                    todo.append(d)
        return seen

    for name, job in jobs.items():
        text = yaml.safe_dump(job)
        pushes = "push=true" in text or "imagetools create" in text
        if not pushes:
            continue
        if "resolve" not in closure(name):
            findings.append(f"job {name} pushes without needing resolve")
        if "imagetools create" in text and "smoke" not in closure(name):
            findings.append(f"job {name} tags before the sidecar suite passed")
        if name == "build" and not str(job.get("if", "")).startswith(
            "needs.resolve.outputs.build == 'true'"
        ):
            findings.append("the build job is not gated on resolve's build output")
        for key in ("build-contexts", "--build-context", "wheels="):
            if key in text:
                findings.append(
                    f"job {name} passes {key}: the image must come from PyPI"
                )
    return findings


def test_the_push_is_gated_on_the_pypi_release() -> None:
    assert gate_findings(_wf(), _load(PUBLISH_WF)) == []


def _plant_ungated_build(doc: dict) -> None:
    doc["jobs"]["build"]["if"] = "always()"


def _plant_local_wheels(doc: dict) -> None:
    for _, step in _steps(doc):
        if "docker/build-push-action" in str(step.get("uses")):
            step["with"]["build-contexts"] = "wheels=dist"


def _plant_tag_before_smoke(doc: dict) -> None:
    doc["jobs"]["publish"]["needs"] = ["resolve", "build"]


def _plant_no_pypi_wait(doc: dict) -> None:
    for step in doc["jobs"]["resolve"]["steps"]:
        if "pip download" in step.get("run", ""):
            step["run"] = "true"


GATE_PLANTS = {
    "an ungated build": _plant_ungated_build,
    "a build from local wheels": _plant_local_wheels,
    "tags moved before the smoke test": _plant_tag_before_smoke,
    "no wait for PyPI": _plant_no_pypi_wait,
}


@pytest.mark.parametrize("plant", GATE_PLANTS.values(), ids=GATE_PLANTS.keys())
def test_a_planted_gate_violation_is_reported(plant) -> None:  # noqa: ANN001
    doc = copy.deepcopy(_wf())
    plant(doc)
    assert gate_findings(doc, _load(PUBLISH_WF))


def test_least_privilege() -> None:
    doc = _wf()
    assert doc["permissions"] == {}
    for name, job in doc["jobs"].items():
        perms = job.get("permissions") or {}
        if perms.get("packages") == "write":
            assert name in {"build", "publish"}, name
        if "id-token" in perms or "attestations" in perms:
            assert name == "publish", name


# ---- registries: each enabled one has its login, its credential check, a leg ----
#
# The registries are DERIVED from docker.yml: the primary one (REGISTRY_NAME,
# always pushed) and one more per workflow-level `<X>_ENABLED` variable.


def _registry_step(doc: dict) -> dict:
    return next(
        s for s in doc["jobs"]["resolve"]["steps"] if s.get("id") == "registries"
    )


def toggled(doc: dict) -> dict[str, str]:
    """Each optional registry's prefix -> its default (`true`/`false`)."""
    out = {}
    for key, value in (doc.get("env") or {}).items():
        m = re.fullmatch(r"([A-Z]+)_ENABLED", key)
        if m:
            default = re.search(r"\|\|\s*'(\w+)'", str(value))
            out[m[1]] = default[1] if default else "false"
    return out


def _gate(prefix: str) -> str:
    return f"env.{prefix}_ENABLED == 'true'"


def _arm(script: str, prefix: str) -> str:
    """The `true)` arm of `case "$<X>_ENABLED"` in the registries step."""
    m = re.search(rf'case "\${prefix}_ENABLED" in(.*?)esac', script, flags=re.S)
    if not m:
        return ""
    arm = re.search(r"^\s*true\)\n(.*?);;", m[1], flags=re.M | re.S)
    return arm[1] if arm else ""


def registry_findings(doc: dict, legs: list[dict]) -> list[str]:
    findings = []
    step = _registry_step(doc)
    script = step["run"]
    step_env = step.get("env") or {}
    for job_name, job in doc["jobs"].items():
        logins = [
            s
            for s in job.get("steps") or []
            if "docker/login-action" in str(s.get("uses"))
        ]
        if not logins:
            continue
        primary = [
            s
            for s in logins
            if "if" not in s and s["with"]["registry"] == "${{ env.REGISTRY_NAME }}"
        ]
        if len(primary) != 1:
            findings.append(
                f"job {job_name} has {len(primary)} primary-registry logins"
            )
        for prefix in toggled(doc):
            mine = [s for s in logins if s.get("if") == _gate(prefix)]
            if len(mine) != 1:
                findings.append(
                    f"{prefix}: job {job_name} has {len(mine)} logins gated on it"
                )
    for prefix, default in toggled(doc).items():
        arm = _arm(script, prefix)
        if not arm:
            findings.append(f"{prefix}: resolve does not handle {prefix}_ENABLED=true")
        secrets = [
            k
            for k, v in step_env.items()
            if k.startswith(f"{prefix}_") and "secrets." in str(v)
        ]
        if not secrets:
            findings.append(f"{prefix}: resolve checks none of its secrets")
        for var in secrets:
            if not re.search(rf'-z "\${var}"', arm) or "exit 1" not in arm:
                findings.append(f"{prefix}: {var} is not refused when missing (exit 1)")
        enabled = [
            leg
            for leg in legs
            if leg["workflow"] == "docker.yml"
            and (leg.get("vars") or {}).get(f"{prefix}_ENABLED", default) == "true"
        ]
        if not enabled:
            findings.append(f"{prefix}: no act dry-run leg enables it")
    return findings


def _legs() -> list[dict]:
    return json.loads(LEGS.read_text(encoding="utf-8"))["legs"]


def test_there_are_registries_to_check() -> None:
    assert "DOCKERHUB" in toggled(_wf())


def test_every_registry_has_its_login_its_credential_check_and_a_leg() -> None:
    assert registry_findings(_wf(), _legs()) == []


def _plant_no_credential_check(doc: dict) -> None:
    step = _registry_step(doc)
    step["run"] = re.sub(
        r'if \[ -z "\$DOCKERHUB_USERNAME" \].*?fi\n', "", step["run"], flags=re.S
    )


def _plant_new_registry(doc: dict) -> None:
    doc["env"]["QUAY_ENABLED"] = "${{ vars.QUAY_ENABLED || 'true' }}"


def _plant_dropped_login(doc: dict) -> None:
    steps = doc["jobs"]["publish"]["steps"]
    doc["jobs"]["publish"]["steps"] = [
        s for s in steps if s.get("if") != _gate("DOCKERHUB")
    ]


def _plant_gated_primary(doc: dict) -> None:
    for s in doc["jobs"]["build"]["steps"]:
        if "docker/login-action" in str(s.get("uses")) and "if" not in s:
            s["if"] = "vars.PRIMARY == 'yes'"


REGISTRY_PLANTS = {
    "a registry enabled with no credential check": _plant_no_credential_check,
    "a registry added with no login, check or leg": _plant_new_registry,
    "a job pushing without that registry's login": _plant_dropped_login,
    "the primary login made conditional": _plant_gated_primary,
}


@pytest.mark.parametrize("plant", REGISTRY_PLANTS.values(), ids=REGISTRY_PLANTS.keys())
def test_a_planted_registry_violation_is_reported(plant) -> None:  # noqa: ANN001
    doc = copy.deepcopy(_wf())
    assert registry_findings(doc, _legs()) == []
    plant(doc)
    assert registry_findings(doc, _legs())


def test_a_registry_off_by_default_without_a_leg_is_reported() -> None:
    doc = copy.deepcopy(_wf())
    doc["env"]["DOCKERHUB_ENABLED"] = "${{ vars.DOCKERHUB_ENABLED || 'false' }}"
    assert registry_findings(doc, _legs())
