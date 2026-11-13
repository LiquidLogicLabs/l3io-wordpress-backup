"""``docker/install-release``: the release install, retried while PyPI's
CDN serves a page from before the upload, and only then, and never unbounded.

The script runs as written under ``sh``, with a stub ``uv`` first on ``PATH``
that answers from a plan, one line per call: ``ok``, ``missing`` (uv's words for
a version the index does not list, wrapped as uv wraps them), ``no-project``,
``only-older``, or ``other`` (any other failure). Every call's arguments are
recorded. The real uv against PyPI is exercised by docker.yml's `resolve` job
and the image build; tests/contracts/test_release_install.py asserts both run
this script.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "docker" / "install-release"

SH = shutil.which("sh")
pytestmark = pytest.mark.skipif(SH is None, reason="needs sh")

# The stub's answers. `missing` is uv 0.11.6's own message, wrapped at a
# different column than the real one so the match cannot depend on the width.
STUB = r"""#!/bin/sh
printf '%s\n' "$*" >> "$STUB_DIR/calls"
n=$(wc -l < "$STUB_DIR/calls")
answer=$(sed -n "${n}p" "$STUB_DIR/plan")
[ -n "$answer" ] || answer=$(tail -n 1 "$STUB_DIR/plan")
case "$answer" in
  ok) echo "Would install 21 packages"; exit 0 ;;
  missing)
    echo "  x No solution found when resolving dependencies:"
    echo "  -> Because there is no version of"
    echo "      l3io-wordpress-backup==1.2.3 and you require"
    echo "      l3io-wordpress-backup==1.2.3,"
    echo "      we can conclude that your requirements are unsatisfiable."
    exit 1 ;;
  no-project)
    echo "  -> Because l3io-wordpress-backup was not found in the package registry"
    echo "      and you require l3io-wordpress-backup==1.2.3, we can conclude that"
    exit 1 ;;
  only-older)
    echo "  -> Because only l3io-wordpress-database==0.3.5 is available and you require"
    echo "      l3io-wordpress-database>=0.3.6, we can conclude that"
    exit 1 ;;
  other)
    echo "error: Failed to fetch: https://pypi.org/simple/x/ (connection reset)"
    exit 2 ;;
esac
echo "stub: unknown plan answer '$answer'" >&2; exit 99
"""

FAST = {
    "INSTALL_RELEASE_DELAY": "0",
    "INSTALL_RELEASE_MAX_DELAY": "0",
    "INSTALL_RELEASE_INTERVAL": "0",
}


def _run(
    tmp_path: Path, plan: list[str], *args: str, **env: str
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    stub = tmp_path / "bin"
    stub.mkdir(exist_ok=True)
    (stub / "uv").write_text(STUB, encoding="utf-8")
    (stub / "uv").chmod(0o755)
    (tmp_path / "plan").write_text("\n".join(plan) + "\n", encoding="utf-8")
    (tmp_path / "calls").unlink(missing_ok=True)
    environ = {
        "PATH": f"{stub}:{os.environ['PATH']}",
        "STUB_DIR": str(tmp_path),
        **env,
    }
    result = subprocess.run(  # noqa: S603 - the repository's own script
        [str(SH), str(SCRIPT), *(args or ("/opt/wpbackup", "1.2.3"))],
        env=environ,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    calls_file = tmp_path / "calls"
    calls = (
        calls_file.read_text(encoding="utf-8").splitlines()
        if calls_file.exists()
        else []
    )
    return result, calls


def test_the_one_invocation_installs_the_release_wheels_only_refreshed(
    tmp_path: Path,
) -> None:
    result, calls = _run(tmp_path, ["ok"])
    assert result.returncode == 0, result.stderr
    assert calls == [
        "pip install --python /opt/wpbackup --no-build --refresh "
        "l3io-wordpress-backup==1.2.3"
    ]


def test_dry_run_is_the_same_invocation_plus_dry_run(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, ["ok"], "--dry-run", "/venv", "1.2.3")
    assert result.returncode == 0, result.stderr
    assert calls == [
        "pip install --python /venv --no-build --refresh --dry-run "
        "l3io-wordpress-backup==1.2.3"
    ]


@pytest.mark.parametrize("stale", ["missing", "no-project", "only-older"])
def test_a_stale_index_page_is_retried_and_each_retry_logged(
    tmp_path: Path, stale: str
) -> None:
    result, calls = _run(tmp_path, [stale, stale, "ok"], **FAST)
    assert result.returncode == 0, result.stderr
    assert len(calls) == 3
    assert "attempt 1: retrying with --refresh" in result.stdout
    assert "attempt 2: retrying with --refresh" in result.stdout


def test_any_other_failure_is_not_retried_and_keeps_uv_s_status(
    tmp_path: Path,
) -> None:
    result, calls = _run(tmp_path, ["other", "ok"], **FAST)
    assert result.returncode == 2
    assert len(calls) == 1
    assert "not for a missing version: not retried" in result.stderr


def test_a_version_that_never_appears_fails_at_the_bound_by_name(
    tmp_path: Path,
) -> None:
    started = time.monotonic()
    result, calls = _run(
        tmp_path,
        ["missing"],
        INSTALL_RELEASE_TIMEOUT="3",
        INSTALL_RELEASE_DELAY="1",
        INSTALL_RELEASE_MAX_DELAY="1",
    )
    elapsed = time.monotonic() - started
    assert result.returncode == 1
    assert "l3io-wordpress-backup==1.2.3 did not install" in result.stderr
    assert "within 3s" in result.stderr
    assert 2 <= len(calls) <= 5
    assert elapsed < 10


def test_the_backoff_doubles_up_to_its_cap(tmp_path: Path) -> None:
    result, _ = _run(
        tmp_path,
        ["missing"] * 4 + ["ok"],
        INSTALL_RELEASE_DELAY="1",
        INSTALL_RELEASE_MAX_DELAY="2",
    )
    assert result.returncode == 0, result.stderr
    waits = [
        line.split(" in ")[1].split("s ")[0]
        for line in result.stdout.splitlines()
        if "retrying with --refresh in" in line
    ]
    assert waits == ["1", "2", "2", "2"]


def test_consecutive_successes_restart_after_a_stale_answer(tmp_path: Path) -> None:
    result, calls = _run(
        tmp_path,
        ["ok", "missing", "ok", "ok", "ok"],
        "--dry-run",
        "/venv",
        "1.2.3",
        INSTALL_RELEASE_SUCCESSES="3",
        **FAST,
    )
    assert result.returncode == 0, result.stderr
    assert len(calls) == 5
    assert "(3 of 3 consecutive)" not in result.stdout  # the last one exits
    assert "(2 of 3 consecutive)" in result.stdout


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("INSTALL_RELEASE_TIMEOUT", "forever"),
        ("INSTALL_RELEASE_TIMEOUT", "-1"),
        ("INSTALL_RELEASE_SUCCESSES", "0"),
        ("INSTALL_RELEASE_DELAY", "1.5"),
    ],
)
def test_a_bad_setting_is_refused_before_uv_runs(
    tmp_path: Path, name: str, value: str
) -> None:
    result, calls = _run(tmp_path, ["ok"], **{name: value})
    assert result.returncode == 2
    assert name in result.stderr
    assert calls == []


@pytest.mark.parametrize("args", [(), ("only-one",), ("a", "b", "c")])
def test_wrong_arguments_are_refused(tmp_path: Path, args: tuple[str, ...]) -> None:
    stub = tmp_path / "bin"
    stub.mkdir()
    result = subprocess.run(  # noqa: S603 - the repository's own script
        [str(SH), str(SCRIPT), *args],
        env={"PATH": f"{stub}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "usage: install-release" in result.stderr
