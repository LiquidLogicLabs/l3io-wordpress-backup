"""E007-S01-003 — the WP-CLI the integration tests run is the supported minimum.

The source of truth for the tested version is ``.github/workflows/ci.yml``: every
step whose ``env`` sets ``WP_CLI_VERSION`` (found by walking every job's steps,
not by step name). ``tests/contracts/test_workflow_invariants.py`` checks that
pin's shape and checksum; this file checks that there is one version, and that
it equals ``adapters/preflight.py``'s ``WP_CLI_MINIMUM``.

``WP_CLI_MINIMUM`` is story E007-S01-004's (7.4). Until it lands, the equality
test is a strict ``xfail`` on the ``ImportError``: when 7.4 adds the constant,
an XPASS fails the suite, so the marker cannot outlive its reason.
"""

from __future__ import annotations

import pathlib
import re
from typing import Any

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
CI = ROOT / ".github" / "workflows" / "ci.yml"


def _pinned_versions(workflow: pathlib.Path = CI) -> list[str]:
    doc: dict[str, Any] = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    found: list[str] = []
    for job in (doc.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            env = step.get("env") or {}
            if "WP_CLI_VERSION" in env:
                found.append(str(env["WP_CLI_VERSION"]))
    return found


def test_ci_pins_exactly_one_wp_cli_version() -> None:
    versions = _pinned_versions()
    assert versions, "no ci.yml step sets WP_CLI_VERSION"
    assert len(set(versions)) == 1, versions
    assert re.fullmatch(r"\d+\.\d+\.\d+", versions[0]), versions[0]


def test_a_planted_second_version_is_found(tmp_path: pathlib.Path) -> None:
    """Scope attack: a step in another job, under another name, is still read."""
    planted = tmp_path / "ci.yml"
    planted.write_text(
        "jobs:\n"
        "  a:\n    steps:\n      - name: one\n        env: {WP_CLI_VERSION: '2.12.0'}\n"
        "  b:\n    steps:\n      - run: x\n        env: {WP_CLI_VERSION: '2.11.0'}\n",
        encoding="utf-8",
    )
    assert _pinned_versions(planted) == ["2.12.0", "2.11.0"]


@pytest.mark.xfail(
    raises=ImportError,
    strict=True,
    reason="WP_CLI_MINIMUM is E007-S01-004's; remove this marker when it lands",
)
def test_the_pinned_version_is_the_supported_minimum() -> None:
    from l3io.wp.backup.adapters.preflight import (  # type: ignore[attr-defined]
        WP_CLI_MINIMUM,
    )

    (pinned,) = set(_pinned_versions())
    assert str(WP_CLI_MINIMUM) == pinned
