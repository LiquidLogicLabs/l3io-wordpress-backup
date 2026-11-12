"""Owner decisions 83, 84 — the image's scheduled prune line and its checks.

``docker/root/usr/local/bin/l3io-wpbackup-crontab`` run as written, with a
stub ``supercronic`` (it only parses the file) on ``PATH``. The image's
defaults (Dockerfile ``ENV``) are read from the Dockerfile, not restated.
The built image runs the same script at start (``tests/sidecar/run.sh``).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "docker" / "root" / "usr" / "local" / "bin" / "l3io-wpbackup-crontab"
DOCKERFILE = ROOT / "docker" / "Dockerfile"
RULES = ("KEEP", "KEEP_DAILY", "KEEP_WEEKLY", "KEEP_MONTHLY", "KEEP_YEARLY")

BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="needs bash")


def _image_defaults() -> dict[str, str]:
    """The ``WPBACKUP_*`` values the Dockerfile sets (comments left out)."""
    lines = DOCKERFILE.read_text(encoding="utf-8").splitlines()
    text = "\n".join(line for line in lines if not line.lstrip().startswith("#"))
    found = re.findall(r'\b(WPBACKUP_[A-Z_]+)=(?:"([^"]*)"|([^\s\\]*))', text)
    return {name: quoted or bare for name, quoted, bare in found}


def _run(tmp_path: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    stub = tmp_path / "bin"
    stub.mkdir(exist_ok=True)
    (stub / "supercronic").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (stub / "supercronic").chmod(0o755)
    environ = {
        "PATH": f"{stub}:{os.environ['PATH']}",
        "WPBACKUP_CRONTAB": "",
        **env,
    }
    return subprocess.run(  # noqa: S603 - the repository's own script
        [str(BASH), str(SCRIPT), str(tmp_path / "crontab")],
        env=environ,
        capture_output=True,
        text=True,
        check=False,
    )


def _job(tmp_path: Path, env: dict[str, str]) -> str:
    result = _run(tmp_path, {"WPBACKUP_SCHEDULE": "0 3 * * *", **env})
    assert result.returncode == 0, result.stderr
    return (tmp_path / "crontab").read_text(encoding="utf-8").strip()


def test_the_image_defaults_are_seven_four_six_and_no_flat_keep() -> None:
    defaults = _image_defaults()
    assert defaults["WPBACKUP_KEEP_DAILY"] == "7"
    assert defaults["WPBACKUP_KEEP_WEEKLY"] == "4"
    assert defaults["WPBACKUP_KEEP_MONTHLY"] == "6"
    assert "WPBACKUP_KEEP_YEARLY" not in defaults
    assert "WPBACKUP_KEEP" not in defaults


def test_the_image_defaults_schedule_the_tiers(tmp_path: Path) -> None:
    assert _job(tmp_path, _image_defaults()) == (
        "0 3 * * * wpbackup2 backup && wpbackup2 prune "
        "--keep-daily 7 --keep-weekly 4 --keep-monthly 6"
    )


def test_only_the_rules_set_are_passed_in_one_order(tmp_path: Path) -> None:
    env = {
        "WPBACKUP_KEEP_YEARLY": "2",
        "WPBACKUP_KEEP": "14",
        "WPBACKUP_KEEP_WEEKLY": "",
        "WPBACKUP_KEEP_MONTHLY": "0",
        "WPBACKUP_KEEP_DAILY": "007",
    }
    assert _job(tmp_path, env) == (
        "0 3 * * * wpbackup2 backup && wpbackup2 prune "
        "--keep 14 --keep-daily 7 --keep-yearly 2"
    )


@pytest.mark.parametrize("value", [None, "", "0", "00"])
def test_every_rule_unset_or_zero_disables_pruning(
    tmp_path: Path, value: str | None
) -> None:
    env = {} if value is None else {f"WPBACKUP_{r}": value for r in RULES}
    assert _job(tmp_path, env) == "0 3 * * * wpbackup2 backup"


@pytest.mark.parametrize("rule", RULES)
@pytest.mark.parametrize("value", ["-1", "seven", "1.5", " 7", "7 ", "1234567890"])
def test_a_bad_value_stops_the_start(tmp_path: Path, rule: str, value: str) -> None:
    result = _run(
        tmp_path, {"WPBACKUP_SCHEDULE": "0 3 * * *", f"WPBACKUP_{rule}": value}
    )
    assert result.returncode == 2
    assert f"WPBACKUP_{rule} must be a whole number" in result.stderr
