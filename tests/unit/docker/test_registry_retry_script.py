"""``scripts/registry-retry``: a command retried, bounded and with backoff, when
it failed with a transient registry error, and only then.

The script runs as written under ``bash``, around a stub command that answers
from a plan, one line per call: ``ok``, ``ok-noisy`` (succeeds, printing a 429
line), ``other`` (fails, exit 3, no registry error), or a transient error
written as BuildKit, containerd or Docker Hub write it (exit 1). A stub
``sleep`` first on ``PATH`` records each wait instead of waiting. Every call's
arguments are recorded. That docker.yml's build steps run through the script
is tests/contracts/test_docker_image.py.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "registry-retry"

BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="needs bash")

# One line per transient signature, as the tools print them.
TRANSIENT = {
    "ghcr-429": (
        "ERROR: failed to build: failed to solve: failed to copy: httpReadSeeker: "
        "failed open: unexpected status from GET request to "
        "https://ghcr.io/v2/linuxserver/baseimage-ubuntu/blobs/sha256:5f6d: "
        "429 Too Many Requests"
    ),
    "hub-toomanyrequests": (
        "toomanyrequests: You have reached your unauthenticated pull rate limit."
    ),
    "500": "unexpected status from HEAD request to https://x/v2/: "
    "500 Internal Server Error",
    "502": "unexpected status from GET request to https://x/v2/: 502 Bad Gateway",
    "503": "received unexpected HTTP status: 503 Service Unavailable",
    "504": "unexpected status from PUT request to https://x/v2/: 504 Gateway Timeout",
    "reset": "read tcp 10.1.0.4:443: read: connection reset by peer",
}

STUB = r"""#!/bin/sh
printf '%s\n' "$*" >> "$STUB_DIR/calls"
n=$(wc -l < "$STUB_DIR/calls")
answer=$(sed -n "${n}p" "$STUB_DIR/plan")
[ -n "$answer" ] || answer=$(tail -n 1 "$STUB_DIR/plan")
case "$answer" in
  ok) echo "built"; exit 0 ;;
  ok-noisy) echo "retrying after 429 Too Many Requests"; echo "built"; exit 0 ;;
  other) echo "ERROR: process \"/bin/sh -c exit 2\" did not complete" >&2; exit 3 ;;
esac
line=$(sed -n "s/^$answer	//p" "$STUB_DIR/transient")
[ -n "$line" ] || { echo "stub: unknown plan answer '$answer'" >&2; exit 99; }
echo "#8 [internal] load metadata"
echo "$line" >&2
exit 1
"""

SLEEP = r"""#!/bin/sh
printf '%s\n' "$1" >> "$STUB_DIR/sleeps"
"""


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _run(
    tmp_path: Path, plan: list[str], *args: str, **env: str
) -> tuple[subprocess.CompletedProcess[str], list[str], list[str]]:
    stub = tmp_path / "bin"
    stub.mkdir(exist_ok=True)
    for name, text in (("flaky", STUB), ("sleep", SLEEP)):
        (stub / name).write_text(text, encoding="utf-8")
        (stub / name).chmod(0o755)
    (tmp_path / "plan").write_text("\n".join(plan) + "\n", encoding="utf-8")
    (tmp_path / "transient").write_text(
        "".join(f"{k}\t{v}\n" for k, v in TRANSIENT.items()), encoding="utf-8"
    )
    for name in ("calls", "sleeps"):
        (tmp_path / name).unlink(missing_ok=True)
    tmp = tmp_path / "tmp"
    tmp.mkdir(exist_ok=True)
    environ = {
        "PATH": f"{stub}:{os.environ['PATH']}",
        "STUB_DIR": str(tmp_path),
        "TMPDIR": str(tmp),
        **env,
    }
    result = subprocess.run(  # noqa: S603 - the repository's own script
        [str(BASH), str(SCRIPT), *(args or ("--", "flaky", "build", "a b"))],
        env=environ,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert list(tmp.iterdir()) == [], "the script left its log behind"
    return result, _lines(tmp_path / "calls"), _lines(tmp_path / "sleeps")


def test_a_success_runs_once_with_the_arguments_as_given(tmp_path: Path) -> None:
    result, calls, sleeps = _run(tmp_path, ["ok"])
    assert result.returncode == 0, result.stderr
    assert calls == ["build a b"]
    assert sleeps == []
    assert "built" in result.stdout


def test_without_the_separator_the_command_runs_the_same(tmp_path: Path) -> None:
    result, calls, _ = _run(tmp_path, ["ok"], "flaky", "x")
    assert result.returncode == 0, result.stderr
    assert calls == ["x"]


@pytest.mark.parametrize("error", TRANSIENT)
def test_a_transient_registry_error_is_retried_and_logged(
    tmp_path: Path, error: str
) -> None:
    result, calls, sleeps = _run(tmp_path, [error, "ok"])
    assert result.returncode == 0, result.stderr
    assert calls == ["build a b"] * 2
    assert sleeps == ["30"]
    assert "attempt 1 of 4 failed (exit 1) with a transient registry error" in (
        result.stderr
    )
    # The command's own output is passed through, failed attempts included.
    assert TRANSIENT[error] in result.stdout


def test_any_other_failure_is_not_retried_and_keeps_its_status(
    tmp_path: Path,
) -> None:
    result, calls, sleeps = _run(tmp_path, ["other", "ok"])
    assert result.returncode == 3
    assert calls == ["build a b"]
    assert sleeps == []
    assert "not with a transient registry error: not retried" in result.stderr


def test_a_success_is_never_retried_whatever_it_printed(tmp_path: Path) -> None:
    result, calls, _ = _run(tmp_path, ["ok-noisy", "other"])
    assert result.returncode == 0, result.stderr
    assert calls == ["build a b"]


def test_a_retry_that_fails_otherwise_is_not_retried_again(tmp_path: Path) -> None:
    """Each attempt is judged by its own output, not an earlier one's."""
    result, calls, _ = _run(tmp_path, ["ghcr-429", "other", "ok"])
    assert result.returncode == 3
    assert len(calls) == 2


def test_the_retries_are_bounded_and_the_backoff_doubles_to_its_cap(
    tmp_path: Path,
) -> None:
    result, calls, sleeps = _run(tmp_path, ["ghcr-429"], REGISTRY_RETRY_ATTEMPTS="5")
    assert result.returncode == 1
    assert len(calls) == 5
    assert sleeps == ["30", "60", "120", "120"]
    assert (
        "attempt 5 of 5 failed (exit 1) with a transient registry error; giving up"
        in (result.stderr)
    )


def test_one_attempt_means_no_retry(tmp_path: Path) -> None:
    result, calls, sleeps = _run(
        tmp_path, ["ghcr-429", "ok"], REGISTRY_RETRY_ATTEMPTS="1"
    )
    assert result.returncode == 1
    assert len(calls) == 1
    assert sleeps == []


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("REGISTRY_RETRY_ATTEMPTS", "0"),
        ("REGISTRY_RETRY_ATTEMPTS", "many"),
        ("REGISTRY_RETRY_DELAY", "-1"),
        ("REGISTRY_RETRY_MAX_DELAY", "1.5"),
    ],
)
def test_a_bad_setting_is_refused_before_the_command_runs(
    tmp_path: Path, name: str, value: str
) -> None:
    result, calls, _ = _run(tmp_path, ["ok"], **{name: value})
    assert result.returncode == 2
    assert name in result.stderr
    assert calls == []


def test_no_command_is_a_usage_error(tmp_path: Path) -> None:
    result, calls, _ = _run(tmp_path, ["ok"], "--")
    assert result.returncode == 2
    assert "usage" in result.stderr
    assert calls == []
