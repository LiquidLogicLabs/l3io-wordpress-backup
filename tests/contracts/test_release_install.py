"""The image's release install and docker.yml's wait for it are ONE invocation.

Run 43501774937 (1.1.1): `resolve` waited for the release with ``pip
download``, saw it, and both image builds then failed in ``uv pip install``
with "there is no version of l3io-wordpress-backup==1.1.1" -- PyPI's CDN
served them a page from before the upload. The wait had checked another
client than the one that installs. So:

* the image's venv stage installs the release only through the script it
  bind-mounts (``docker/install-release``), which retries a missing
  version, bounded, and nothing else;
* docker.yml's `resolve` job waits by running THAT script (the path derived
  from the Dockerfile's mount and the workflow's build context) with
  ``--dry-run``, with the uv the Dockerfile pins (read from its ``UV_IMAGE``),
  no cache, requiring consecutive successes within a bound;
* nothing in any Dockerfile or any docker.yml step installs or downloads the
  release by another route.

The scope is derived: every Dockerfile in the tree, every step of every job
in docker.yml. Each rule has a planted violation, including ones aimed at the
scope (a second Dockerfile, another job, a copy of the script elsewhere). The
script's own behaviour (what it retries, the bound) is
tests/unit/docker/test_install_release_script.py.
"""

from __future__ import annotations

import copy
import pathlib
import re
import subprocess
from collections.abc import Callable

import pytest
import yaml

from tests.contracts.test_docker_image import _instructions

ROOT = pathlib.Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "docker" / "Dockerfile"
DOCKER_WF = ROOT / ".github" / "workflows" / "docker.yml"
SKIP_PARTS = {".git", ".venv", "node_modules", "legacy", "_bmad", "_bmad-output"}

PACKAGE = "l3io-wordpress-backup"
# Any pip-style install or download naming the release, by any client.
OTHER_ROUTE = re.compile(rf"\bpip3?\s+(?:install|download)\b[^\n;|&]*\b{PACKAGE}\b")
MOUNT = re.compile(r"--mount=type=bind,source=([^,\s]+),target=([^,\s]+)")
# The bound on the whole wait, and on the build's retry (seconds).
MAX_WAIT = 1800
MAX_BUILD_RETRY = 900


def _dockerfiles() -> dict[str, str]:
    return {
        str(p.relative_to(ROOT)): p.read_text(encoding="utf-8")
        for p in sorted(ROOT.rglob("Dockerfile*"))
        if p.is_file() and not SKIP_PARTS.intersection(p.relative_to(ROOT).parts)
    }


def _wf() -> dict:
    return yaml.safe_load(DOCKER_WF.read_text(encoding="utf-8"))


def _script_text(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def install_script(dockerfile: str) -> tuple[str, str] | None:
    """The (context-relative source, target) the venv RUN mounts and runs."""
    for ins in _instructions(dockerfile):
        if not ins.upper().startswith("RUN ") or "uv venv" not in ins:
            continue
        for source, target in MOUNT.findall(ins):
            if f'sh {target} /opt/wpbackup "$VERSION"' in ins:
                return source, target
    return None


def build_context(doc: dict) -> str | None:
    """The context the build job's push builds: the last word of its
    ``docker buildx build ... -f "$DOCKER_FILE" <context>``."""
    for step in doc["jobs"]["build"]["steps"]:
        run = str(step.get("run", ""))
        if "push=true" in run:
            m = re.search(r'-f "\$DOCKER_FILE" (\S+)\s*$', run, flags=re.M)
            return m[1] if m else None
    return None


def findings(dockerfiles: dict[str, str], doc: dict, script: str) -> list[str]:
    found: list[str] = []
    product = dockerfiles.get("docker/Dockerfile", "")

    # 1. Nothing installs the release by another route, anywhere in scope.
    for where, text in dockerfiles.items():
        for ins in _instructions(text):
            if OTHER_ROUTE.search(ins):
                found.append(f"{where}: installs {PACKAGE} other than via the script")
    for job_name, job in doc["jobs"].items():
        for step in job.get("steps") or []:
            if OTHER_ROUTE.search(step.get("run", "")):
                found.append(
                    f"docker.yml {job_name}/{step.get('name')}: fetches {PACKAGE} "
                    "other than via the script"
                )

    # 2. The image installs the release through the mounted script.
    mounted = install_script(product)
    if mounted is None:
        found.append(
            "docker/Dockerfile: the venv RUN does not install via a mounted script"
        )
        return found
    source, _ = mounted
    if "UV_NO_CACHE=1" not in product:
        found.append("docker/Dockerfile: the build's uv is not run without a cache")
    if "INSTALL_RELEASE_TIMEOUT" in product:
        found.append("docker/Dockerfile: overrides the script's bound")

    # 3. The script is bounded by default.
    default = re.search(r"INSTALL_RELEASE_TIMEOUT:-(\d+)\}", script)
    if not default or not 0 < int(default.group(1)) <= MAX_BUILD_RETRY:
        found.append(f"the script's default bound is not 1..{MAX_BUILD_RETRY}s")

    # 4. resolve waits with the same script, the same uv, no cache, bounded.
    context = build_context(doc)
    path = f"{context}/{source}"
    waits = [
        s
        for s in doc["jobs"]["resolve"]["steps"]
        if f"sh {path} --dry-run" in s.get("run", "")
    ]
    if len(waits) != 1:
        found.append(f"resolve does not wait by running {path} --dry-run (once)")
        return found
    wait = waits[0]
    env = {k: str(v) for k, v in (wait.get("env") or {}).items()}
    run = wait["run"]
    if (
        not env.get("INSTALL_RELEASE_SUCCESSES", "").isdigit()
        or int(env["INSTALL_RELEASE_SUCCESSES"]) < 2
    ):
        found.append("resolve's wait does not require consecutive successes")
    timeout = env.get("INSTALL_RELEASE_TIMEOUT", "")
    if not timeout.isdigit() or not 0 < int(timeout) <= MAX_WAIT:
        found.append(f"resolve's wait is not bounded to 1..{MAX_WAIT}s")
    if env.get("UV_NO_CACHE") != "1":
        found.append("resolve's wait runs uv with a cache")
    if """sed -n 's/^ARG UV_IMAGE=//p' "$DOCKER_FILE\"""" not in run or (
        'docker cp "$cid:/uv"' not in run
    ):
        found.append("resolve's wait does not take uv from the Dockerfile's UV_IMAGE")
    resolve_text = yaml.safe_dump(doc["jobs"]["resolve"])
    for other in ("setup-uv", "pip install uv", "astral.sh/uv"):
        if other in resolve_text:
            found.append(f"resolve gets uv another way ({other})")
    return found


def _current() -> tuple[dict[str, str], dict, str]:
    return _dockerfiles(), _wf(), _script_text(ROOT / "docker" / "install-release")


def test_the_scope_is_not_empty() -> None:
    dockerfiles, doc, script = _current()
    assert "docker/Dockerfile" in dockerfiles
    assert install_script(dockerfiles["docker/Dockerfile"]) is not None
    assert build_context(doc) == "docker"
    assert script


def test_the_wait_and_the_build_are_one_invocation() -> None:
    assert findings(*_current()) == []


def test_the_script_named_by_the_mount_exists_in_the_build_context() -> None:
    dockerfiles, doc, _ = _current()
    mounted = install_script(dockerfiles["docker/Dockerfile"])
    assert mounted is not None
    path = ROOT / str(build_context(doc)) / mounted[0]
    assert path.is_file()
    # And git would commit it: a script under an ignored name (it once sat in
    # docker/build/, which .gitignore's `build` hides) works here and is
    # missing from the checkout the workflow builds.
    ignored = subprocess.run(  # noqa: S603 - git over our own path
        ["git", "check-ignore", "-q", str(path)],  # noqa: S607
        cwd=ROOT,
        check=False,
    )
    assert ignored.returncode == 1, f"{path.relative_to(ROOT)} is git-ignored"


# ---- planted violations ------------------------------------------------------

_INVOKE = 'sh /usr/local/bin/install-release /opt/wpbackup "$VERSION"'
_DIRECT = (
    "uv pip install --python /opt/wpbackup --no-build "
    '"l3io-wordpress-backup==${VERSION}"'
)


def _resolve_wait(doc: dict) -> dict:
    return next(
        s for s in doc["jobs"]["resolve"]["steps"] if "--dry-run" in s.get("run", "")
    )


def _p_direct_install(d: dict[str, str], doc: dict, s: str) -> None:
    d["docker/Dockerfile"] = d["docker/Dockerfile"].replace(_INVOKE, _DIRECT)


def _p_second_dockerfile(d: dict[str, str], doc: dict, s: str) -> None:
    d["docker/Dockerfile.alt"] = f"FROM scratch\nRUN {_DIRECT}\n"


def _p_mount_renamed(d: dict[str, str], doc: dict, s: str) -> None:
    d["docker/Dockerfile"] = d["docker/Dockerfile"].replace(
        "source=install-release", "source=install-release-v2"
    )


def _p_pip_download_wait(d: dict[str, str], doc: dict, s: str) -> None:
    _resolve_wait(doc)["run"] = (
        'python3 -m pip download -q --no-deps -d x "l3io-wordpress-backup==$VERSION"\n'
    )


def _p_copy_elsewhere(d: dict[str, str], doc: dict, s: str) -> None:
    w = _resolve_wait(doc)
    w["run"] = w["run"].replace(
        "sh docker/install-release", "sh scripts/install-release"
    )


def _p_one_success(d: dict[str, str], doc: dict, s: str) -> None:
    _resolve_wait(doc)["env"]["INSTALL_RELEASE_SUCCESSES"] = 1


def _p_unbounded_wait(d: dict[str, str], doc: dict, s: str) -> None:
    del _resolve_wait(doc)["env"]["INSTALL_RELEASE_TIMEOUT"]


def _p_day_long_wait(d: dict[str, str], doc: dict, s: str) -> None:
    _resolve_wait(doc)["env"]["INSTALL_RELEASE_TIMEOUT"] = 86400


def _p_cached_uv(d: dict[str, str], doc: dict, s: str) -> None:
    del _resolve_wait(doc)["env"]["UV_NO_CACHE"]


def _p_another_uv(d: dict[str, str], doc: dict, s: str) -> None:
    w = _resolve_wait(doc)
    w["run"] = "pip install uv==0.11.6\n" + re.sub(r".*UV_IMAGE.*\n", "", w["run"])


def _p_another_job_downloads(d: dict[str, str], doc: dict, s: str) -> None:
    doc["jobs"]["smoke"]["steps"].append(
        {"run": 'pip download --no-deps "l3io-wordpress-backup==$VERSION"'}
    )


def _p_build_overrides_bound(d: dict[str, str], doc: dict, s: str) -> None:
    d["docker/Dockerfile"] = d["docker/Dockerfile"].replace(
        "UV_NO_CACHE=1", "UV_NO_CACHE=1 INSTALL_RELEASE_TIMEOUT=86400"
    )


PLANTS: dict[str, Callable[[dict[str, str], dict, str], None]] = {
    "the Dockerfile installs directly": _p_direct_install,
    "a second Dockerfile installs directly": _p_second_dockerfile,
    "the mount names another script": _p_mount_renamed,
    "the wait is pip download again": _p_pip_download_wait,
    "the wait runs a copy of the script": _p_copy_elsewhere,
    "one success is enough": _p_one_success,
    "the wait has no bound": _p_unbounded_wait,
    "the wait is a day long": _p_day_long_wait,
    "the wait's uv has a cache": _p_cached_uv,
    "the wait's uv is not the Dockerfile's": _p_another_uv,
    "another job downloads the release": _p_another_job_downloads,
    "the build overrides the bound": _p_build_overrides_bound,
}


@pytest.mark.parametrize("plant", PLANTS.values(), ids=PLANTS.keys())
def test_a_planted_violation_is_reported(
    plant: Callable[[dict[str, str], dict, str], None],
) -> None:
    dockerfiles, doc, script = _current()
    dockerfiles = dict(dockerfiles)
    doc = copy.deepcopy(doc)
    plant(dockerfiles, doc, script)
    assert findings(dockerfiles, doc, script)


@pytest.mark.parametrize("default", ["0", "86400"])
def test_a_planted_script_bound_is_reported(default: str) -> None:
    dockerfiles, doc, script = _current()
    planted = script.replace(
        "INSTALL_RELEASE_TIMEOUT:-600}", f"INSTALL_RELEASE_TIMEOUT:-{default}}}"
    )
    assert planted != script
    assert findings(dockerfiles, doc, planted)
