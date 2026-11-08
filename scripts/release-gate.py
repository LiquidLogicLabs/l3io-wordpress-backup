#!/usr/bin/env python3
"""The release gate `make release` runs before it dispatches anything.

Family rule (Shawn, 2026-11-08): nothing is released, tagged or published until
CI is fully green on GitHub AND `make preflight` has passed locally for that
commit. This refuses unless both hold for HEAD:

1. the preflight record (`git rev-parse --git-path l3io-preflight`, written only
   by a passing `make preflight`) names HEAD's full SHA;
2. GitHub has runs for HEAD's full SHA, every one of them is `completed` with
   conclusion `success`, and every workflow that a push to the release branch
   triggers is among them.

The required workflows are DERIVED from `.github/workflows/*.yml` (their own
`on.push.branches`), never listed here, so a workflow added later is required
without an edit -- and "CI has not started yet" cannot pass as green just
because the faster security audit already finished.

Coverage, stated: the gate reads the runs `gh run list --commit` returns (up to
RUN_LIMIT). It does not inspect individual jobs; a run's conclusion is GitHub's
summary of them. Dispatching release.yml by hand bypasses this gate.

Exit 0 = release may proceed. Exit 1 = refused, with the reason on stdout.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import yaml

STAMP = "l3io-preflight"
RUN_LIMIT = 200


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def required_workflows(root: pathlib.Path, branch: str) -> set[str]:
    """Names of the workflows a push to `branch` triggers."""
    names: set[str] = set()
    for path in sorted((root / ".github" / "workflows").glob("*.y*ml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        # PyYAML reads the bare key `on` as boolean True.
        on = doc.get("on", doc.get(True)) or {}
        if isinstance(on, str):
            on = {on: None}
        elif isinstance(on, list):
            on = dict.fromkeys(on)
        if "push" not in on:
            continue
        push = on["push"] or {}
        branches = push.get("branches")
        if push.get("tags") and branches is None:
            continue  # tag pushes only
        if branches is None or branch in branches:
            names.add(str(doc.get("name") or path.name))
    return names


def refuse(msg: str) -> int:
    print(f"release refused: {msg}")
    return 1


def main() -> int:
    gh = os.environ.get("GH", "gh")
    branch = os.environ.get("RELEASE_BRANCH", "main")
    root = pathlib.Path(_git("rev-parse", "--show-toplevel"))
    sha = _git("rev-parse", "HEAD")
    short = sha[:12]

    stamp = pathlib.Path(_git("rev-parse", "--git-path", STAMP))
    if not stamp.is_absolute():
        stamp = pathlib.Path.cwd() / stamp
    recorded = stamp.read_text(encoding="utf-8").strip() if stamp.is_file() else ""
    if recorded != sha:
        return refuse(
            f"no passing `make preflight` recorded for {short} -- run it first"
        )

    required = required_workflows(root, branch)
    if not required:
        return refuse(
            f"no workflow runs on a push to {branch}; "
            "the CI check would pass over nothing"
        )

    listed = subprocess.run(
        [
            gh,
            "run",
            "list",
            "--commit",
            sha,
            "--limit",
            str(RUN_LIMIT),
            "--json",
            "workflowName,status,conclusion",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if listed.returncode != 0:
        return refuse(
            f"could not list GitHub runs for {short}: {listed.stderr.strip()}"
        )
    try:
        runs = json.loads(listed.stdout or "[]")
    except json.JSONDecodeError:
        return refuse(f"`{gh} run list` did not return JSON")
    if not runs:
        return refuse(
            f"GitHub has no runs for {short} yet -- CI must have run and passed"
        )

    red = [
        f"{r.get('workflowName')}: {r.get('status')}/{r.get('conclusion') or '-'}"
        for r in runs
        if r.get("status") != "completed" or r.get("conclusion") != "success"
    ]
    if red:
        return refuse(f"CI on GitHub is not green for {short}:\n  " + "\n  ".join(red))
    missing = sorted(required - {str(r.get("workflowName")) for r in runs})
    if missing:
        return refuse(f"no GitHub run for {short} of: {', '.join(missing)}")

    print(
        f"release gate passed for {short}: preflight recorded; "
        f"{len(runs)} GitHub run(s) green, including {', '.join(sorted(required))}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
