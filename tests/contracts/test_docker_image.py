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
from collections.abc import Callable

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


def _credentials_job(doc: dict) -> tuple[str, dict]:
    """The job whose step ``credentials`` checks each enabled registry's login.

    Not resolve: resolve's outputs are the image names, and GitHub drops a job
    output containing the value of any secret the job references.
    """
    return next(
        (name, job)
        for name, job in doc["jobs"].items()
        if any(s.get("id") == "credentials" for s in job.get("steps") or [])
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
    script = _registry_step(doc)["run"]
    cred_name, cred_job = _credentials_job(doc)
    cred_step = next(s for s in cred_job["steps"] if s.get("id") == "credentials")
    cred_env = cred_step.get("env") or {}
    build_needs = doc["jobs"]["build"].get("needs") or []
    if cred_name not in (
        [build_needs] if isinstance(build_needs, str) else build_needs
    ):
        findings.append(f"build does not need {cred_name}: it may push unchecked")
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
        cred_arm = _arm(cred_step["run"], prefix)
        creds = {
            k: str(v)
            for k, v in cred_env.items()
            if k.startswith(f"{prefix}_")
            and ("secrets." in str(v) or "vars." in str(v))
        }
        if not any("secrets." in v for v in creds.values()):
            findings.append(f"{prefix}: {cred_name} checks none of its secrets")
        for var in creds:
            if not re.search(rf'-z "\${var}"', cred_arm) or "exit 1" not in cred_arm:
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
    _, job = _credentials_job(doc)
    step = next(s for s in job["steps"] if s.get("id") == "credentials")
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


def _plant_build_skips_credentials(doc: dict) -> None:
    doc["jobs"]["build"]["needs"] = ["resolve"]


REGISTRY_PLANTS = {
    "a registry enabled with no credential check": _plant_no_credential_check,
    "build not waiting for the credential check": _plant_build_skips_credentials,
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


# ---- image variants (owner decision 81): each scanned, attested, smoked -------
#
# The variant dimension's one source is docker.yml's `build` matrix (`variant`,
# `db_client`); the clients it must cover are the Dockerfile's own DB_CLIENT
# choices (its `case`). From those, every other place that names a variant is
# checked: build's per-variant digest output, its scan and its push (the
# client passed, SBOM, provenance), the smoke legs (each variant on at least
# one engine, a client on its own family's server), the publish legs (suffixed
# tags, the floating tag, the attestation of that variant's digest, the
# anonymous pull), ci.yml's `image` job (every client built and smoked) and the
# Makefile's `image-smoke` legs (the same as docker.yml's smoke legs).

CI_WF = ROOT / ".github" / "workflows" / "ci.yml"
MAKEFILE = ROOT / "Makefile"
DIGEST_REF = "needs.build.outputs[format('digest_{0}', matrix.variant)]"


def dockerfile_clients(text: str) -> set[str]:
    """The DB_CLIENT values the Dockerfile's ``case`` accepts."""
    return set(re.findall(r"^\s*(\w+)\)\s+client=", text, flags=re.M))


def _family(client: str) -> str:
    return "mariadb" if client == "mariadb" else "mysql"


def _engine_family(engine: str) -> str:
    return "mariadb" if engine.startswith("mariadb") else "mysql"


def _matrix_legs(matrix: dict) -> list[dict]:
    """A matrix's legs: the cross product less ``exclude``, plus ``include``."""
    axes = {k: v for k, v in matrix.items() if k not in {"include", "exclude"}}
    legs: list[dict] = [{}] if axes else []
    for key, values in axes.items():
        legs = [{**leg, key: v} for leg in legs for v in values]
    for gone in matrix.get("exclude") or []:
        legs = [leg for leg in legs if any(leg.get(k) != v for k, v in gone.items())]
    return legs + list(matrix.get("include") or [])


def makefile_legs(text: str) -> set[tuple[str, str]]:
    m = re.search(r"^IMAGE_LEGS \?= (.*)$", text, flags=re.M)
    if not m:
        return set()
    return {tuple(leg.split(":", 1)) for leg in m[1].split()}  # type: ignore[misc]


def variant_findings(doc: dict, ci: dict, dockerfile: str, makefile: str) -> list[str]:
    findings: list[str] = []
    jobs = doc["jobs"]
    build = jobs["build"]
    variants = {
        leg["variant"]: leg["db_client"]
        for leg in _matrix_legs(build.get("strategy", {}).get("matrix") or {})
        if "variant" in leg and "db_client" in leg
    }
    if not variants:
        return ["docker.yml build has no variant matrix"]
    clients = dockerfile_clients(dockerfile)
    if not clients:
        findings.append("the Dockerfile's DB_CLIENT choices could not be read")
    for client in sorted(clients - set(variants.values())):
        findings.append(f"DB_CLIENT={client} is built by no variant")
    for client in sorted(set(variants.values()) - clients):
        findings.append(
            f"a variant builds DB_CLIENT={client}, which the Dockerfile refuses"
        )
    # build: a digest output per variant, from the push step only.
    outputs = build.get("outputs") or {}
    for variant in variants:
        expr = str(outputs.get(f"digest_{variant}", ""))
        if (
            f"matrix.variant == '{variant}'" not in expr
            or "steps.push.outputs.digest" not in expr
        ):
            findings.append(f"build has no digest_{variant} output from its push")
    steps = build.get("steps") or []
    scan = [s for s in steps if "trivy" in str(s.get("run", ""))]
    if not scan or not any(
        str((s.get("env") or {}).get("DB_CLIENT")) == "${{ matrix.db_client }}"
        and '--build-arg "DB_CLIENT=$DB_CLIENT"' in s["run"]
        and "--ignore-unfixed --exit-code 1" in s["run"]
        for s in scan
    ):
        findings.append("build's scan does not build and gate each variant's client")
    push = [s for s in steps if s.get("id") == "push"]
    with_ = (push[0].get("with") or {}) if push else {}
    if "DB_CLIENT=${{ matrix.db_client }}" not in str(with_.get("build-args", "")):
        findings.append("build's push does not pass the variant's DB_CLIENT")
    if with_.get("sbom") is not True or "mode=max" not in str(with_.get("provenance")):
        findings.append("build's push has no SBOM or no max provenance")
    # smoke: each variant on at least one engine, a client on its own family.
    smoke = jobs["smoke"]
    smoke_legs = _matrix_legs(smoke.get("strategy", {}).get("matrix") or {})
    for variant, client in variants.items():
        engines = {leg["engine"] for leg in smoke_legs if leg.get("variant") == variant}
        if not engines:
            findings.append(f"smoke never runs the {variant} variant")
        elif _family(client) not in {_engine_family(e) for e in engines}:
            findings.append(
                f"smoke never runs the {variant} variant on a {_family(client)} server"
            )
    if any(leg.get("variant") not in variants for leg in smoke_legs):
        findings.append("smoke runs a variant build does not build")
    if DIGEST_REF not in yaml.safe_dump(smoke, width=10**6):
        findings.append("smoke does not test each variant's own digest")
    # publish: per variant, its suffixed tags, attestation and anonymous pull.
    publish = jobs["publish"]
    publish_legs = _matrix_legs(publish.get("strategy", {}).get("matrix") or {})
    published = {leg.get("variant"): leg for leg in publish_legs}
    for variant in variants:
        leg = published.get(variant)
        if leg is None:
            findings.append(f"publish never tags the {variant} variant")
            continue
        suffix, floating = (
            ("", "latest") if variant == "default" else (f"-{variant}", variant)
        )
        if leg.get("suffix") != suffix or leg.get("floating") != floating:
            findings.append(
                f"publish tags {variant} as "
                f"{leg.get('suffix')!r}/{leg.get('floating')!r}"
            )
    if set(published) - set(variants):
        findings.append("publish tags a variant build does not build")
    if str((publish.get("env") or {}).get("DIGEST")) != "${{ " + DIGEST_REF + " }}":
        findings.append("publish does not take each variant's own digest")
    meta = next(
        (
            s
            for s in publish.get("steps") or []
            if "docker/metadata-action" in str(s.get("uses"))
        ),
        {},
    )
    tags = str((meta.get("with") or {}).get("tags", "")).strip().splitlines()
    semver = [t for t in tags if t.startswith("type=semver")]
    if len(semver) != 3 or not all(
        t.endswith("suffix=${{ matrix.suffix }}") for t in semver
    ):
        findings.append("publish's semver tags do not all carry the variant's suffix")
    if not any(t.startswith("type=raw,value=${{ matrix.floating }}") for t in tags):
        findings.append("publish's floating tag is not the variant's")
    attests = [
        s
        for s in publish.get("steps") or []
        if "attest-build-provenance" in str(s.get("uses"))
    ]
    if not attests or any(
        s["with"].get("subject-digest") != "${{ env.DIGEST }}" for s in attests
    ):
        findings.append(
            "publish does not attest each variant's digest in every registry"
        )
    if not any(
        "anonymously" in str(s.get("name", "")) for s in publish.get("steps") or []
    ):
        findings.append("publish does not check each variant's tags pull anonymously")
    # ci.yml image job: every client built from the checkout and smoked.
    ci_legs = _matrix_legs(ci["jobs"]["image"].get("strategy", {}).get("matrix") or {})
    for client in sorted(clients):
        engines = {
            leg.get("engine", "") for leg in ci_legs if leg.get("client") == client
        }
        if _family(client) not in {_engine_family(e) for e in engines}:
            findings.append(
                f"ci.yml image never smokes the {client} client on its own family"
            )
    # the Makefile's image-smoke legs are docker.yml's smoke legs.
    want = {
        (variants[leg["variant"]], leg["engine"])
        for leg in smoke_legs
        if leg.get("variant") in variants
    }
    if makefile_legs(makefile) != want:
        findings.append(
            f"Makefile IMAGE_LEGS {sorted(makefile_legs(makefile))} != "
            f"docker.yml smoke {sorted(want)}"
        )
    return findings


def _variant_inputs() -> tuple[dict, dict, str, str]:
    return (
        _wf(),
        _load(CI_WF),
        DOCKERFILE.read_text(encoding="utf-8"),
        MAKEFILE.read_text(encoding="utf-8"),
    )


def test_there_are_variants_to_check() -> None:
    doc, _ci, dockerfile, _make = _variant_inputs()
    assert dockerfile_clients(dockerfile) == {"mysql", "mariadb"}
    assert {
        leg["variant"]
        for leg in _matrix_legs(doc["jobs"]["build"]["strategy"]["matrix"])
    } == {
        "default",
        "mariadb",
    }


def test_every_variant_is_scanned_attested_and_smoked() -> None:
    assert variant_findings(*_variant_inputs()) == []


def _drop(job: str, variant: str) -> Callable[[dict, dict, list[str]], None]:
    def plant(doc: dict, _ci: dict, _text: list[str]) -> None:
        matrix = doc["jobs"][job]["strategy"]["matrix"]
        if "variant" in matrix:
            matrix["variant"] = [v for v in matrix["variant"] if v != variant]
        matrix["include"] = [
            i for i in matrix.get("include") or [] if i.get("variant") != variant
        ]

    return plant


def _plant_new_client(_doc: dict, _ci: dict, text: list[str]) -> None:
    text[0] = text[0].replace(
        "mariadb) client=mariadb-client ;;",
        "mariadb) client=mariadb-client ;; \\\n"
        "      percona) client=percona-server-client ;;",
    )


def _plant_variant_without_output(doc: dict, _ci: dict, _text: list[str]) -> None:
    doc["jobs"]["build"]["outputs"].pop("digest_mariadb")


def _plant_scan_without_client(doc: dict, _ci: dict, _text: list[str]) -> None:
    for step in doc["jobs"]["build"]["steps"]:
        if "trivy" in str(step.get("run", "")):
            step["run"] = step["run"].replace('--build-arg "DB_CLIENT=$DB_CLIENT"', "")


def _plant_unsuffixed_tags(doc: dict, _ci: dict, _text: list[str]) -> None:
    for step in doc["jobs"]["publish"]["steps"]:
        if "docker/metadata-action" in str(step.get("uses")):
            step["with"]["tags"] = step["with"]["tags"].replace(
                ",suffix=${{ matrix.suffix }}", ""
            )


def _plant_mariadb_only_on_mysql(doc: dict, _ci: dict, _text: list[str]) -> None:
    doc["jobs"]["smoke"]["strategy"]["matrix"]["exclude"] = [
        {"variant": "mariadb", "engine": "mariadb"}
    ]


def _plant_ci_drops_a_client(_doc: dict, ci: dict, _text: list[str]) -> None:
    matrix = ci["jobs"]["image"]["strategy"]["matrix"]
    matrix["include"] = [i for i in matrix["include"] if i.get("client") != "mariadb"]


def _plant_makefile_drift(_doc: dict, _ci: dict, text: list[str]) -> None:
    text[1] = re.sub(
        r"^(IMAGE_LEGS \?= .*?) mariadb:mariadb", r"\1", text[1], flags=re.M
    )


def _plant_shared_digest(doc: dict, _ci: dict, _text: list[str]) -> None:
    doc["jobs"]["publish"]["env"]["DIGEST"] = (
        "${{ needs.build.outputs.digest_default }}"
    )


VARIANT_PLANTS = {
    "a Dockerfile client no variant builds": _plant_new_client,
    "a variant without its digest output": _plant_variant_without_output,
    "a scan that ignores the client": _plant_scan_without_client,
    "the mariadb variant never smoked": _drop("smoke", "mariadb"),
    "the mariadb variant never published": _drop("publish", "mariadb"),
    "a client smoked only on the other family": _plant_mariadb_only_on_mysql,
    "suffix-less tags (a variant overwrites the default)": _plant_unsuffixed_tags,
    "every variant published from one digest": _plant_shared_digest,
    "ci.yml no longer smokes a client": _plant_ci_drops_a_client,
    "the Makefile's legs drift from docker.yml's": _plant_makefile_drift,
}


@pytest.mark.parametrize("plant", VARIANT_PLANTS.values(), ids=VARIANT_PLANTS.keys())
def test_a_planted_variant_violation_is_reported(plant) -> None:  # noqa: ANN001
    doc, ci, dockerfile, makefile = _variant_inputs()
    assert variant_findings(doc, ci, dockerfile, makefile) == []
    texts = [dockerfile, makefile]
    plant(doc, ci, texts)
    assert variant_findings(doc, ci, texts[0], texts[1])
