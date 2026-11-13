"""Every shell script in the repository passes ShellCheck.

The scope is derived, not listed: every file git tracks or would track
(untracked but not ignored), outside ``legacy/``, whose first line is a ``sh``,
``bash`` or ``dash`` shebang -- whatever its name (the image's scripts have no
``.sh``). ShellCheck is the ``shellcheck-py`` wheel from the test dependency
group, so the check runs the same on GitHub, under act (whose runner image has
no shellcheck) and locally.

Not covered: shell embedded in workflow ``run:`` blocks, the Makefile and the
Dockerfile's ``RUN`` lines.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SKIP_PARTS = {"legacy"}
SHELLS = {"sh", "bash", "dash"}


def _shellcheck() -> str:
    """The venv's shellcheck (shellcheck-py), else one on PATH."""
    bindir = pathlib.Path(sys.executable).parent
    found = shutil.which("shellcheck", path=str(bindir)) or shutil.which("shellcheck")
    assert found, "shellcheck is missing: `uv sync` installs shellcheck-py (test group)"
    return found


def is_shell(first_line: str) -> bool:
    """A shebang any of whose words names a shell: ``#!/bin/sh``,
    ``#!/usr/bin/env bash``, ``#!/usr/bin/with-contenv bash``."""
    if not first_line.startswith("#!"):
        return False
    return any(w.rsplit("/", 1)[-1] in SHELLS for w in first_line[2:].split())


def shell_scripts(root: pathlib.Path) -> list[pathlib.Path]:
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],  # noqa: S607
        cwd=root,
        capture_output=True,
        check=True,
    ).stdout.decode()
    found = []
    for rel in sorted(set(filter(None, listed.split("\0")))):
        path = root / rel
        if SKIP_PARTS.intersection(pathlib.PurePath(rel).parts) or not path.is_file():
            continue
        with path.open("rb") as fh:
            first = fh.readline(256).decode("utf-8", "replace")
        if is_shell(first):
            found.append(path)
    return found


def shellcheck(paths: list[pathlib.Path]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - the pinned shellcheck over our own files
        [_shellcheck(), "--format=gcc", *map(str, paths)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_scope_has_the_scripts_the_image_and_ci_run() -> None:
    found = {str(p.relative_to(ROOT)) for p in shell_scripts(ROOT)}
    for expected in (
        "docker/install-release",
        "docker/root/usr/local/bin/l3io-wpbackup-entrypoint",
        "tests/sidecar/run.sh",
    ):
        assert expected in found


def test_every_shell_script_passes_shellcheck() -> None:
    result = shellcheck(shell_scripts(ROOT))
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("line", "shell"),
    [
        ("#!/bin/sh\n", True),
        ("#!/bin/bash -eu\n", True),
        ("#!/usr/bin/env bash\n", True),
        ("#!/usr/bin/env -S bash -e\n", True),
        ("#!/usr/bin/dash\n", True),
        ("#!/usr/bin/env python3\n", False),
        ("#!/usr/bin/with-contenv bash\n", True),
        ("# not a shebang: sh\n", False),
    ],
)
def test_the_scope_is_decided_by_the_shebang(line: str, *, shell: bool) -> None:
    assert is_shell(line) is shell


def test_a_planted_violation_is_reported(tmp_path: pathlib.Path) -> None:
    script = tmp_path / "no-extension"
    script.write_text("#!/bin/sh\nrm -rf $1/*\n[ $x == y ]\n", encoding="utf-8")
    result = shellcheck([script])
    assert result.returncode != 0
    assert "SC2086" in result.stdout


def test_a_planted_untracked_script_is_in_scope(tmp_path: pathlib.Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)  # noqa: S603, S607
    (tmp_path / "tool").write_text("#!/usr/bin/env bash\necho $1\n", encoding="utf-8")
    (tmp_path / "legacy").mkdir()
    (tmp_path / "legacy" / "old").write_text("#!/bin/sh\necho $1\n", encoding="utf-8")
    (tmp_path / "x.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    assert [p.name for p in shell_scripts(tmp_path)] == ["tool"]
