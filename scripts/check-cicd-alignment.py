#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Conformance check for the l3io Python-package CI/CD standard.

Checks every sibling package repo, not a hand-kept list: the scope is derived by
globbing for `pyproject.toml` + `.github/workflows/` under the parent directory.
A repo added tomorrow is checked tomorrow, without editing this file -- a
hand-maintained roster is exactly how a guard ends up reporting success over a
set that no longer contains what matters.

Exit 0 = all repos conform. Exit 1 = at least one finding.
"""

from __future__ import annotations

import argparse
import contextlib
import pathlib
import re
import sys
from collections.abc import Callable
from typing import Any

import yaml

# Records one finding: (area, message).
Reporter = Callable[[str, str], None]


def load(p: pathlib.Path) -> Any:  # noqa: ANN401 - whatever the YAML holds
    with p.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def triggers(doc: dict) -> dict:
    # PyYAML parses a bare `on:` key as boolean True
    return doc.get(True, doc.get("on")) or {}


def jobtext(job: dict) -> str:
    return yaml.safe_dump(job, default_flow_style=False)


def code_only(job: dict) -> str:
    """Job text with shell comments stripped.

    A `run:` block's comments are part of the serialized job, so a naive grep
    finds a rule's own name in the comment that says the rule is being FOLLOWED.
    That produced two false positives on the first run of this checker: a comment
    reading "not pyproject.toml, because this job has no checkout" was reported as
    reading pyproject.toml.
    """
    out = []
    for line in jobtext(job).splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#") or stripped.startswith("\\n#"):
            continue
        out.append(re.sub(r"\\n\s*#[^\\]*", " ", line))
    return "\n".join(out)


def strip_comments(blob: str) -> str:
    """Remove YAML/shell comments from a serialized workflow.

    A `run:` block's comments survive serialization, so a rule that greps the blob
    finds its own subject inside the comment that says the rule is satisfied.
    """
    out = []
    for line in blob.splitlines():
        s = line.lstrip()
        if s.startswith("#"):
            continue
        out.append(re.sub(r"\\n\s*#[^\\]*", " ", line))
    return "\n".join(out)


def needs_closure(jobs: dict, start: str) -> set[str]:
    """Every job transitively reachable through `needs` from `start`."""
    seen: set[str] = set()
    stack = list(jobs.get(start, {}).get("needs") or [])
    while stack:
        n = stack.pop()
        if n in seen or n not in jobs:
            continue
        seen.add(n)
        stack += list(jobs[n].get("needs") or [])
    return seen


# ---------------------------------------------------------------- rules


def check_ci(doc: dict, R: Reporter) -> str:
    on, jobs = triggers(doc), doc.get("jobs", {})
    if "pull_request" not in on:
        R("ci", "no pull_request trigger -- CI never gates a PR")
    push = on.get("push") or {}
    if isinstance(push, dict) and "main" not in (push.get("branches") or []):
        R("ci", f"push trigger is {push.get('branches')}, expected ['main']")
    if "workflow_dispatch" not in on:
        R("ci", "no workflow_dispatch trigger -- cannot be re-run by hand")
    for j in ("static", "test", "verify-install"):
        if j not in jobs:
            R("ci", f"missing required job `{j}`")
    blob = yaml.safe_dump(doc)
    if (
        "verify-install" in jobs
        and "--no-project" not in blob
        and "clean" not in blob.lower()
    ):
        R("ci", "verify-install present but does not look like a clean-install check")
    return blob


def check_namespace(ci_blob: str, pub_blob: str, is_ns: bool, R: Reporter) -> None:
    """AD-27: a namespace package must be guarded at BOTH ends."""
    if not is_ns:
        return
    shadow = r"__init__\.py"
    if not re.search(shadow, ci_blob):
        R(
            "ns",
            "ci.yml has no AD-27 __init__.py shadow guard -- a namespace break "
            "reaches publish instead of the PR",
        )
    # Do NOT match the word "namespace": it appears in comments, so a file saying
    # "we should check the namespace" and nothing else would pass. That is the
    # comment-stripping hazard this checker warned another repo about, in this
    # checker's own rule -- found by that repo reading it. Require the structural
    # thing a real check must mention outside a comment.
    if "__init__.py" not in strip_comments(pub_blob):
        R(
            "ns",
            "publish.yml has no structural namespace assertion (no __init__.py "
            "check outside a comment) -- the word alone is not a check",
        )


def check_version_single_source(repo: pathlib.Path, R: Reporter) -> None:
    """The git TAG is the version: no literal anywhere, and the vcs wiring intact.

    Shawn's ruling, after both the two-literal shape and the literal+dynamic shape
    were implemented and rejected: if the tag is the version there is nothing to
    keep in sync, so the agreement test a second literal needs is solving a problem
    that should not exist.

    The literal rule carves out a module that also reads importlib.metadata. Without
    that carve-out it flags `_version.py`'s own "0.0.0+unknown" sentinel -- a sibling
    session's first audit did exactly that and reported all three repos as carrying
    literals, which is how a correct arrangement gets a rule switched off.
    """
    pyproject = repo / "pyproject.toml"
    if not pyproject.is_file():
        return
    try:
        import tomllib

        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except Exception as e:
        R("ver", f"pyproject.toml unreadable, so version rules were not checked: {e}")
        return

    project = data.get("project", {})
    hatch = data.get("tool", {}).get("hatch", {}).get("version", {}) or {}
    raw = hatch.get("raw-options", {}) or {}

    if "version" in project:
        R(
            "ver",
            f"pyproject.toml carries a static version literal ({project['version']}); "
            "the tag is the source",
        )
    if "version" not in (project.get("dynamic") or []):
        R("ver", 'pyproject.toml does not declare dynamic = ["version"]')
    if hatch.get("source") != "vcs":
        R(
            "ver",
            f'[tool.hatch.version] source is {hatch.get("source")!r}, expected "vcs" '
            "(a `path` source reads a literal again)",
        )
    if "hatch-vcs" not in str(data.get("build-system", {}).get("requires", [])):
        R("ver", "build-system.requires does not include hatch-vcs")
    if raw.get("local_scheme") != "no-local-version":
        R(
            "ver",
            "raw-options.local_scheme is not no-local-version -- a +g<sha> local "
            "segment is rejected by PyPI and most indexes",
        )
    cmd = str(raw.get("git_describe_command", ""))
    if not re.search(r"--match\s+v\[0-9\]\*\.\[0-9\]\*\.\[0-9\]\*", cmd):
        R(
            "ver",
            "git_describe_command does not --match three-component v tags -- a "
            "two-component tag like v1.0 is read as the basis and guessed to "
            "1.1.devN, which sorts ABOVE 1.0.0",
        )

    # A literal is allowed only where metadata is also read.
    offenders = []
    for f in sorted((repo / "src").rglob("*.py")) if (repo / "src").is_dir() else []:
        text = f.read_text(encoding="utf-8", errors="ignore")
        if not re.search(r'^\s*__version__(\s*:\s*str)?\s*=\s*["\']', text, re.M):
            continue
        if "importlib.metadata" in text or "importlib_metadata" in text:
            continue
        offenders.append(f.relative_to(repo).as_posix())
    if offenders:
        R("ver", f"version literal outside a metadata-reading module: {offenders}")


_REF_CACHE: dict[str, bool] = {}


def check_release_dispatch(repo: pathlib.Path, R: Reporter) -> None:
    """release.yml must hand the new tag to publish.yml on the REAL-release path.

    Reported by py-wpconfigr. This repo's release.yml had its only dispatch step
    guarded `if: inputs.bump == 'dev'`, so `bump=patch|minor|major` created the
    tag, moved the floating tags, wrote "Publish.yml is now building it" to the
    step summary -- and dispatched nothing. A tag created with GITHUB_TOKEN fires
    no push event either, so nothing downstream picked it up. It reported success
    and published nothing, and had never misfired only because the repo has no v*
    tags yet.

    The nine act dry-run legs could not catch this: a dry run proves the steps
    that EXIST run, and the defect is a step that does not exist. An absence is
    invisible to a replay.

    Reads the COMMITTED file: a sibling mid-edit is not a specification.
    """
    import subprocess as _s

    r = _s.run(
        ["git", "-C", str(repo), "show", "HEAD:.github/workflows/release.yml"],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        return  # absence is reported by check_release_process
    try:
        doc = yaml.safe_load(r.stdout)
    except yaml.YAMLError as e:
        R("disp", f"release.yml is not valid YAML at HEAD: {str(e).splitlines()[0]}")
        return
    steps = [
        s for j in (doc.get("jobs") or {}).values() for s in (j.get("steps") or [])
    ]
    dispatches = [
        s
        for s in steps
        if "trigger-workflow" in str(s.get("uses", ""))
        or "workflow run" in str(s.get("run", ""))
    ]
    if not dispatches:
        R(
            "disp",
            "release.yml never dispatches publish.yml -- it creates a tag and "
            "nothing builds it (a tag created with GITHUB_TOKEN fires no push event)",
        )
        return
    # At least one must be reachable when bump is NOT dev.
    real = [
        s
        for s in dispatches
        if "!= 'dev'" in str(s.get("if", "")).replace('"', "'") or not s.get("if")
    ]
    if not real:
        guards = [str(s.get("if")) for s in dispatches]
        R(
            "disp",
            "release.yml dispatches publish.yml ONLY on the dev path "
            f"({guards}) -- bump=patch|minor|major tags a release and publishes "
            "nothing, while the summary claims otherwise",
        )


def check_action_refs(repo: pathlib.Path, R: Reporter, *, online: bool) -> None:
    """Every `uses:` ref must EXIST, and org actions must be pinned to a major.

    This rule exists because all three repos referenced
    `LiquidLogicLabs/git-action-changelog-parser@v3.0.14` and there is no v3 --
    the action's latest release is v2.1.5. Every release in every repo would have
    failed with "Unable to resolve action". Nothing in any workflow, test or
    checker noticed, because a version string reads as authoritative whether or
    not it resolves. That is the documented-flag-that-does-not-exist failure in a
    different costume.

    Needs network, so it degrades to a skip rather than a false pass: when the
    lookup cannot run, it says so instead of reporting the refs as fine.
    """
    import subprocess

    refs: dict[str, set[str]] = {}
    for f in sorted((repo / ".github" / "workflows").glob("*.yml")):
        for m in re.finditer(
            r"uses:\s*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)@([^\s\"']+)",
            f.read_text(encoding="utf-8"),
        ):
            refs.setdefault(m.group(1), set()).add(m.group(2))

    for action, tags in sorted(refs.items()):
        is_org = action.startswith("LiquidLogicLabs/")
        if is_org and len(tags) > 1:
            R(
                "act",
                f"{action} is pinned at {sorted(tags)} in the same repo -- "
                "one major per action",
            )
        for tag in sorted(tags):
            if is_org and not re.fullmatch(r"v[0-9]+", tag):
                R("act", f"{action}@{tag} is not a floating major; pin to vN")
            if not online:
                continue
            key = f"{action}@{tag}"
            if key not in _REF_CACHE:
                # A branch-style ref (pypa's release/v1) is not a tag; try both.
                ok = False
                for kind in ("tags", "heads"):
                    r = subprocess.run(
                        ["gh", "api", f"repos/{action}/git/ref/{kind}/{tag}"],
                        capture_output=True,
                    )
                    if r.returncode == 0:
                        ok = True
                        break
                _REF_CACHE[key] = ok
            if not _REF_CACHE[key]:
                R("act", f"{key} DOES NOT EXIST -- the workflow cannot resolve it")


def check_release_process(repo: pathlib.Path, R: Reporter) -> None:
    """release.yml, `make release`, and a changelog gate that runs BEFORE the tag.

    Shawn's call is three hand-maintained copies of the workflow for now rather than
    a shared workflow_call. That makes this checker the only thing preventing drift,
    which is the whole reason these rules are mechanical and not a paragraph in a
    README.
    """
    rel = repo / ".github" / "workflows" / "release.yml"
    if not rel.is_file():
        R("rel", "no .github/workflows/release.yml -- no single release path")

    mk = repo / "Makefile"
    if not mk.is_file():
        R("rel", "no Makefile")
    else:
        mk_text = mk.read_text(encoding="utf-8")
        if not re.search(r"^release(-patch)?\s*:", mk_text, re.M):
            R("rel", "Makefile has no `release` target (`make release patch`)")
        # GREP FOR THE MECHANISM, NOT THE LABEL. A sibling session audited all three
        # repos with `grep '^ci-dryrun:'` and this repo matched -- because it had
        # had a `ci-dryrun` target for a while, the old one-event `act push
        # --dryrun` that segfaulted. The check asserted a target NAME, not the
        # implementation behind it, and would have reported three greens over one
        # working target.
        if (
            re.search(r"^ci-dryrun\s*:", mk_text, re.M)
            and "act-dryrun-legs" not in mk_text
        ):
            R(
                "rel",
                "`ci-dryrun` exists but does not read .github/act-dryrun-legs.json -- "
                "a one-event dry run skips the jobs the event does not reach, so the "
                "action refs in those jobs are never resolved",
            )
        if "release" in mk_text and "--untracked-files=no" not in mk_text:
            R(
                "rel",
                "the release guard does not pass --untracked-files=no; these repos "
                "permanently carry untracked BMad directories, so a bare "
                "`git status --porcelain` is never empty and would block every release",
            )

    # RELEASE NOTES COME FROM COMMIT MESSAGES, not a CHANGELOG.md.
    #
    # The hand-maintained file was dropped across all three repos: it needed a
    # manual edit before every release, nothing kept `## [Unreleased]` or the
    # link refs current, and commitizen was rejected for silently deleting 85
    # lines of one repo's hand-written content. So the commit log is the single
    # source and the org action builds the notes from it.
    blobs = []
    for name in ("release.yml", "publish.yml"):
        f = repo / ".github" / "workflows" / name
        if f.is_file():
            blobs.append(f.read_text(encoding="utf-8"))
    wf = "\n".join(blobs)

    if "git-action-release-changelog-builder" not in wf:
        R(
            "rel",
            "nothing builds release notes from the commit log "
            "(git-action-release-changelog-builder)",
        )
    if "git-action-changelog-parser" in strip_comments(wf):
        R(
            "rel",
            "still uses git-action-changelog-parser, which READS a CHANGELOG.md -- "
            "the notes now come from commit messages",
        )
    if (repo / "CHANGELOG.md").is_file():
        R(
            "rel",
            "CHANGELOG.md still exists; release notes are built from commits, so a "
            "second hand-maintained source will drift from them",
        )
    if "git-action-release@" in wf and "outputs.changelog" not in wf:
        R("rel", "the release step does not use the built notes as its body")


def check_publish(doc: dict, R: Reporter) -> str:
    on, jobs = triggers(doc), doc.get("jobs", {})
    push = on.get("push") or {}
    if not (
        isinstance(push, dict)
        and any(t.startswith("v") for t in (push.get("tags") or []))
    ):
        R("pub", "no `push: tags: [v*]` trigger")
    if "release" not in on:
        R(
            "pub",
            "no `release: types: [published]` trigger -- "
            "cutting a release does not publish",
        )
    wd = on.get("workflow_dispatch") or {}
    opts = (((wd.get("inputs") or {}).get("target") or {}).get("options")) or []
    for want in ("testpypi", "pypi", "mirror"):
        if want not in opts:
            R("pub", f"workflow_dispatch target option `{want}` missing (has {opts})")

    for j in (
        "version-gate",
        "build",
        "smoke",
        "publish-pypi",
        "publish-mirror",
        "release",
    ):
        if j not in jobs:
            R("pub", f"missing required job `{j}`")

    # --- the secrets-in-if trap
    for name, job in jobs.items():
        cond = str(job.get("if", ""))
        if "secrets." in cond:
            R(
                "pub",
                f"job `{name}` references `secrets` in its `if:` -- not an available "
                "context there; it evaluates empty and SILENTLY SKIPS the job",
            )

    # --- mirror must be gated on a preflight boolean, not on vars alone
    pre = next((n for n in jobs if n.endswith("preflight")), None)
    if pre is None:
        R(
            "pub",
            "no preflight job -- mirror cannot be gated on secret presence, because "
            "`secrets` is unavailable in a job-level `if:`",
        )
    else:
        if pre != "preflight":
            R(
                "pub",
                f"preflight job is named `{pre}`; the standard name is `preflight`",
            )
        outs = jobs[pre].get("outputs") or {}
        if not outs:
            R(
                "pub",
                f"`{pre}` declares no outputs -- nothing for the mirror to gate on",
            )
        if "${VAR:+set}" not in jobtext(jobs[pre]) and ":+set}" not in jobtext(
            jobs[pre]
        ):
            R(
                "pub",
                f"`{pre}` does not use ${{VAR:+set}} -- a skip is not diagnosable "
                "from the log without leaking a value",
            )

    mir = jobs.get("publish-mirror")
    if mir:
        cond = str(mir.get("if", ""))
        if pre and f"needs.{pre}.outputs" not in cond:
            R("pub", "publish-mirror is not gated on the preflight output")
        if "inputs.target" not in cond and "event_name" not in cond:
            R(
                "pub",
                "publish-mirror has no event/target gate -- a testpypi dry run also "
                "publishes to the mirror",
            )
        body = jobtext(mir)
        if "check-url" in body and "curl" not in body:
            R(
                "pub",
                "mirror relies on `uv publish --check-url` for idempotence; "
                "it does not provide it (the check gets no credentials "
                "and falls through to a 409)",
            )
        if "curl" not in body:
            R("pub", "publish-mirror has no idempotence pre-check")
        if "pyproject" in code_only(mir):
            R(
                "pub",
                "mirror derives identity from pyproject.toml; this job has no checkout",
            )

    rel = jobs.get("release")
    if rel:
        needs = rel.get("needs") or []
        if any("mirror" in n for n in needs):
            R(
                "pub",
                "release depends on a mirror job -- "
                "a red mirror then blocks the release",
            )
        cond = str(rel.get("if", ""))
        c = cond.replace('"', "'")
        # Either form correctly excludes a `release` event: an explicit exclusion,
        # or restricting to push (which is stricter). Checking only for the literal
        # spelling of one of them was a false positive on the first run.
        excluded = ("event_name != 'release'" in c) or ("event_name == 'push'" in c)
        if "release" in str(on) and not excluded:
            R(
                "pub",
                "release job does not exclude `release` events, but `release:` is a "
                "trigger -- it will try to create a release that already exists",
            )
        c2 = cond.replace('"', "'")
        # `if: !cancelled()` unquoted is invalid YAML (`!` starts a tag), so the real
        # spellings are ${{ !cancelled() }} or a quoted scalar. Match on the function
        # name with optional whitespace rather than one literal form.
        forced = bool(
            re.search(r"always\s*\(\s*\)", c2)
            or re.search(r"!\s*cancelled\s*\(\s*\)", c2)
        )
        guards = re.findall(r"needs\.[A-Za-z0-9_-]+\.result", c2)
        # Fire only when a job that CAN be skipped is actually in the release job's
        # needs closure. The previous spelling fired on every release job, which
        # encoded this repo's dependency graph as universal and was a false positive
        # against a release job that needs only unconditional jobs -- reported by
        # py-wordpress-database, verified by walking the closure.
        conditional = sorted(
            n for n in needs_closure(jobs, "release") if jobs[n].get("if")
        )
        if conditional and not forced:
            R(
                "pub",
                f"release can be skipped: {conditional} in its needs closure carry an "
                "`if:`, and a skipped dependency skips release. Needs always() or "
                "!cancelled() to stay evaluated",
            )
        # Independent of topology: a force function WITHOUT per-need result guards is
        # actively harmful. Neither always() NOR !cancelled() filters a FAILED
        # dependency -- any truthy `if:` overrides failure propagation -- so the force
        # function alone cuts a release for a wheel that just failed its checks. Only
        # explicit needs.<job>.result assertions distinguish skipped from failed.
        if forced and not guards:
            R(
                "pub",
                "release `if:` forces evaluation (always()/!cancelled()) "
                "but asserts no "
                "needs.<job>.result -- a FAILED dependency would still cut a release. "
                "Neither function filters failure; only result checks do",
            )
    return yaml.safe_dump(doc)


# ------------------------------------------------- F1: aligned 2026-10-27
#
# Four alignments config and database landed (config 1e9a9d0, database d0d0cb9)
# and Shawn approved enforcing here. Each was holding by memory; each check below
# was proved by planting the violation it exists to catch.


def check_floating_tags(rel_doc: dict, R: Reporter) -> None:
    """release.yml moves v<major> and v<major>.<minor> AFTER the tag, BEFORE publish.

    Moved before the tag exists, the floating tag points at the previous release;
    moved after the publish dispatch, a consumer following v<major> can be handed a
    release whose publish has not started.
    """
    for name, job in (rel_doc.get("jobs") or {}).items():
        steps = job.get("steps") or []
        uses = [str(st.get("uses", "")) for st in steps]
        create = [i for i, u in enumerate(uses) if "git-action-tag-create-update" in u]
        if not create:
            continue
        floating = [
            i for i, u in enumerate(uses) if "git-action-tag-floating-version" in u
        ]
        dispatch = [
            i
            for i, u in enumerate(uses)
            if "git-action-trigger-workflow" in u and i > create[0]
        ]
        if not floating:
            R(
                "rel",
                f"release.yml job `{name}` creates a tag "
                "but never moves the floating tags",
            )
            continue
        if floating[0] < create[0]:
            R(
                "rel",
                f"release.yml job `{name}` moves floating tags BEFORE creating the tag",
            )
        if dispatch and floating[0] > dispatch[0]:
            R(
                "rel",
                f"release.yml job `{name}` moves floating tags "
                "AFTER dispatching publish",
            )
        return
    R("rel", "release.yml has no step using git-action-tag-create-update")


def check_publish_shape(pub_doc: dict, R: Reporter) -> None:
    """version-gate reads the tag; the release job is gated, not merely ordered.

    The `needs` list is NOT compared literally: config and database differ there
    legitimately (database runs AD-27 inside smoke). What is checked is that the
    release cannot be cut when a gate failed or publish-pypi failed or was cancelled.
    """
    jobs = pub_doc.get("jobs") or {}
    gate = jobs.get("version-gate") or {}
    if not any(
        "git-action-tag-info" in str(st.get("uses", ""))
        for st in gate.get("steps") or []
    ):
        R("pub", "version-gate does not read the tag with git-action-tag-info")
    rel = jobs.get("release") or {}
    needs = rel.get("needs") or []
    needs = [needs] if isinstance(needs, str) else list(needs)
    cond = " ".join(str(rel.get("if", "")).split())
    for gate_job in ("smoke", "publish-pypi"):
        if gate_job not in needs:
            R("pub", f"release does not need `{gate_job}`")
    for gate_job in [n for n in needs if n not in ("publish-pypi",)]:
        if f"needs.{gate_job}.result == 'success'" not in cond:
            R("pub", f"release `if:` does not require `{gate_job}` to succeed")
    rejects = (
        "needs.publish-pypi.result != 'failure'" in cond
        and "needs.publish-pypi.result != 'cancelled'" in cond
    ) or (
        "needs.publish-pypi.result == 'success'" in cond
        and "needs.publish-pypi.result == 'skipped'" in cond
    )
    if not rejects:
        R(
            "pub",
            "release `if:` does not reject a failed or cancelled publish-pypi",
        )


#: ruff rules every family repo selects on top of its own base, and what tests
#: may ignore. Config and database already carry exactly these.
RUFF_REQUIRED = {"ARG", "TRY", "EM", "S"}
RUFF_TEST_IGNORES = {"S101", "S105", "S106", "S107", "ARG"}


def check_ruff(repo: pathlib.Path, R: Reporter) -> None:
    import tomllib

    try:
        ruff = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        R("ruff", f"pyproject.toml unreadable, so the ruff set was not checked: {e}")
        return
    lint = ((ruff.get("tool") or {}).get("ruff") or {}).get("lint") or {}
    missing = RUFF_REQUIRED - set(lint.get("select") or [])
    if missing:
        R("ruff", f"[tool.ruff.lint] select lacks {sorted(missing)}")
    pfi = lint.get("per-file-ignores") or {}
    tests = [set(v) for k, v in pfi.items() if k.startswith("tests/")]
    if not tests or not any(t >= RUFF_TEST_IGNORES for t in tests):
        R(
            "ruff",
            f"no tests/* per-file-ignores carrying {sorted(RUFF_TEST_IGNORES)}",
        )


#: Job-level env naming host ports a job binds OUTSIDE `services:` -- e.g. a step
#: that runs `docker run -p 127.0.0.1:33399:3306`. Agreed with database 2026-10-27.
HOST_PORTS_ENV = "L3IO_ACT_HOST_PORTS"
_DOCKER_PUBLISH = re.compile(r"\bdocker\s+run\b[^\n]*\s(?:-p|--publish)[\s=]")


def check_act_matrix(ci_doc: dict, R: Reporter) -> None:
    """Under act, collapse an axis only where its legs would collide on a host port.

    act runs `--network host`. A job with `services` starts every leg's containers
    up front, so the legs collide and the axis MUST be collapsed. A job that binds
    host ports from inside a step declares them in `L3IO_ACT_HOST_PORTS`; its legs
    start containers one at a time under `make ci`'s `--concurrent-jobs 1`, so a
    collapse is PERMITTED but not required. A job with neither has nothing to
    collide on, and collapsing it only stops act checking what the axis covers.

    Coverage, stated rather than implied: an undeclared `docker run -p` written
    directly in a step's `run:` is reported. One reached through `make` or a script
    is NOT visible here -- the declaration is the only signal for those.
    """
    for name, job in (ci_doc.get("jobs") or {}).items():
        env = job.get("env") or {}
        declared = [
            p.strip()
            for p in str(env.get(HOST_PORTS_ENV) or "").split(",")
            if p.strip()
        ]
        bad = [p for p in declared if not p.isdigit()]
        if bad:
            R(
                "act",
                f"ci.yml job `{name}` {HOST_PORTS_ENV} holds non-port values {bad}",
            )
        run_code = "\n".join(
            line
            for st in job.get("steps") or []
            for line in str(st.get("run", "")).splitlines()
            if not line.lstrip().startswith("#")
        )
        if not declared and _DOCKER_PUBLISH.search(run_code):
            R(
                "act",
                f"ci.yml job `{name}` publishes a host port with `docker run -p` "
                f"but does not declare it in {HOST_PORTS_ENV}",
            )
        matrix = (job.get("strategy") or {}).get("matrix") or {}
        axes = {k: str(v) for k, v in matrix.items() if k not in ("include", "exclude")}
        has_services = bool(job.get("services"))
        for axis, expr in axes.items():
            collapses = "vars.LOCAL_ACT" in expr
            if has_services and not collapses:
                R(
                    "act",
                    f"ci.yml job `{name}` has services but axis `{axis}` is not "
                    "collapsed under vars.LOCAL_ACT -- legs collide on the host port",
                )
            if not has_services and not declared and collapses:
                R(
                    "act",
                    f"ci.yml job `{name}` binds no host port but collapses axis "
                    f"`{axis}` under vars.LOCAL_ACT -- act stops checking what the "
                    "axis covers",
                )


def in_scope(repo: pathlib.Path) -> str | None:
    """None when `repo` is a family package; otherwise why it is skipped.

    Derived from the source of truth -- the shared namespace a family package ships
    -- not from a directory glob or a name prefix. A family repo that loses its
    namespace is therefore printed as skipped, not silently dropped.
    """
    wp = repo / "src" / "l3io" / "wp"
    if not wp.is_dir():
        return "not an l3io.wp package (no src/l3io/wp/)"
    if not any(d.is_dir() and not d.name.startswith("__") for d in wp.iterdir()):
        return "not an l3io.wp package (src/l3io/wp/ holds no <area>)"
    return None


# ---------------------------------------------------------------- driver


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--offline",
        action="store_true",
        help="skip the action-ref existence lookups (they need gh + network)",
    )
    ap.add_argument(
        "--root",
        default=str(pathlib.Path(__file__).resolve().parents[2]),
        help="directory holding the package repos (default: this repo's parent)",
    )
    a = ap.parse_args()
    root = pathlib.Path(a.root)

    candidates = sorted(
        p.parent
        for p in root.glob("*/pyproject.toml")
        if (p.parent / ".github" / "workflows").is_dir()
    )
    skipped = [(c, why) for c in candidates if (why := in_scope(c)) is not None]
    repos = [c for c in candidates if in_scope(c) is None]
    for c, why in skipped:
        print(f"SKIP {c.name}: {why}")
    if not repos:
        print(f"no package repos found under {root}", file=sys.stderr)
        return 2

    import subprocess as _sp

    online = (
        not a.offline
        and _sp.run(["gh", "auth", "status"], capture_output=True).returncode == 0
    )
    if not online:
        print(
            "NOTE action-ref existence is NOT being checked "
            f"({'--offline' if a.offline else 'gh unavailable or unauthenticated'}). "
            "A ref that does not resolve will not be reported."
        )

    total = 0
    for repo in repos:
        findings: list[tuple[str, str]] = []
        R: Reporter = lambda area, msg, findings=findings: findings.append(  # noqa: E731
            (area, msg)
        )
        wf = repo / ".github" / "workflows"
        ci_p, pub_p = wf / "ci.yml", wf / "publish.yml"
        ci_blob = pub_blob = ""
        # A parse failure is a finding, not an exception. Letting it raise ended the
        # whole sweep at the first bad file, so repos after it were never examined --
        # the checker stopped covering its own scope, which is the failure it exists
        # to catch in others. Found by feeding it a workflow with invalid YAML.
        if not ci_p.exists():
            R("ci", "no ci.yml")
        else:
            try:
                ci_blob = check_ci(load(ci_p), R)
            except yaml.YAMLError as e:
                R(
                    "ci",
                    f"ci.yml is not valid YAML, so nothing in it was checked: "
                    f"{str(e).splitlines()[0]}",
                )
        if not pub_p.exists():
            R("pub", "no publish.yml")
        else:
            try:
                pub_blob = check_publish(load(pub_p), R)
            except yaml.YAMLError as e:
                R(
                    "pub",
                    f"publish.yml is not valid YAML, so nothing in it was checked: "
                    f"{str(e).splitlines()[0]}",
                )

        is_ns = (repo / "src" / "l3io").is_dir()
        check_namespace(ci_blob, pub_blob, is_ns, R)
        check_ruff(repo, R)
        for path, check in (
            (ci_p, check_act_matrix),
            (pub_p, check_publish_shape),
            (wf / "release.yml", check_floating_tags),
        ):
            if path.is_file():
                # A parse failure is reported once, by the pass that owns the file.
                with contextlib.suppress(yaml.YAMLError):
                    check(load(path), R)
        check_version_single_source(repo, R)
        check_release_process(repo, R)
        check_action_refs(repo, R, online=online)
        check_release_dispatch(repo, R)

        # tracked-in-git check: a workflow that is not committed never runs
        import subprocess

        for f in sorted(wf.glob("*.yml")):
            r = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "ls-files",
                    "--error-unmatch",
                    str(f.relative_to(repo)),
                ],
                capture_output=True,
            )
            if r.returncode != 0:
                R(
                    "git",
                    f"{f.name} is NOT tracked in git -- it can never run on a push",
                )

        mark = "OK" if not findings else f"{len(findings)} finding(s)"
        ns = " [namespace pkg]" if is_ns else ""
        print(f"\n=== {repo.name}{ns} -- {mark}")
        for area, msg in findings:
            print(f"  [{area}] {msg}")
        total += len(findings)

    # CROSS-REPO: distribution name and namespace, checked INDEPENDENTLY.
    #
    # The two are not derivable from each other and must not be treated as if they
    # were. Distribution is `l3io-wordpress-<area>`; the import namespace is
    # `l3io.wp.<area>`. ADR-0012's table originally listed them side by side in a
    # way that reads as a derivation rule, and a sibling session acted on that
    # inference during the rename -- moved src/l3io/wp to src/l3io/wordpress and
    # broke AD-27 immediately, because renaming one of three means the three share
    # no namespace at all. This repo made the same inference an hour later.
    #
    # The namespace is SHARED, so no single repo can change it alone. That is why
    # this rule lives here, across the set, and not in the per-repo pass.
    names: dict[str, str] = {}
    namespaces: dict[str, str] = {}
    for repo in repos:
        # READ THE COMMITTED FILE, NOT THE WORKING TREE.
        #
        # This checker sweeps SIBLING repos, and a sibling is routinely mid-change.
        # Reading their working tree reported py-wordpress-database as already
        # renamed to l3io-wordpress-database while its committed pyproject still
        # said l3io-wp-database -- so the family rule went green on a state no
        # clone could see, and acting on it broke `uv lock`. Third instance of the
        # same error in one session: a sibling's working tree is not a
        # specification.
        import subprocess as _s

        r = _s.run(
            ["git", "-C", str(repo), "show", "HEAD:pyproject.toml"],
            capture_output=True,
            text=True,
        )
        content = r.stdout if r.returncode == 0 else ""
        if content:
            m = re.search(r'^name = "([^"]+)"', content, re.M)
            if m:
                names[repo.name] = m.group(1)
        areas = (
            [
                d.name
                for d in (repo / "src" / "l3io").iterdir()
                if d.is_dir() and not d.name.startswith("__")
            ]
            if (repo / "src" / "l3io").is_dir()
            else []
        )
        if areas:
            namespaces[repo.name] = f"l3io/{areas[0]}"
    fam = []
    for r, n in sorted(names.items()):
        if not re.fullmatch(r"l3io-wordpress-[a-z0-9-]+", n):
            fam.append(f"{r}: distribution is {n!r}, expected l3io-wordpress-<area>")
    for r, ns in sorted(namespaces.items()):
        if ns != "l3io/wp":
            fam.append(
                f"{r}: namespace root is {ns!r}, expected l3io/wp "
                f"(it is SHARED -- one repo cannot move it alone)"
            )
    if fam:
        print(f"\n=== family (cross-repo) -- {len(fam)} finding(s)")
        for f in fam:
            print(f"  [fam] {f}")
        total += len(fam)

    # CROSS-REPO: the first-release version must agree across the family.
    # Reported after the sweep because it is a property of the SET, not of any one
    # repo -- and this checker missed exactly that: two repos said v0.0.1 while a
    # third still said v1.0.0, and every per-repo rule passed.
    firsts: dict[str, str] = {}
    for repo in repos:
        rel = repo / ".github" / "workflows" / "release.yml"
        if not rel.is_file():
            continue
        m = re.search(
            r"the first release is (v[0-9]+\.[0-9]+\.[0-9]+)",
            rel.read_text(encoding="utf-8"),
        )
        if m:
            firsts[repo.name] = m.group(1)
    if len(set(firsts.values())) > 1:
        print("\n=== family (cross-repo) -- 1 finding(s)")
        print(
            f"  [fam] the first-release version disagrees across the family: {firsts}. "
            "These packages release together; a divergent starting version is a gap "
            "no per-repo rule can see."
        )
        total += 1

    print(f"\n{'-' * 60}\n{len(repos)} repo(s) checked, {total} finding(s)")
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
