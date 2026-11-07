"""AD-8 — ``l3io.wp.backup.api`` imports and refuses cleanly in a base install.

A subprocess blocks ``l3io.wp.database`` and ``opentelemetry.sdk`` in
``sys.modules`` before the first import, which is what an environment without
the ``database`` and ``otel`` extras looks like to an ``import``. There:

- ``import l3io.wp.backup.api`` succeeds and every name in ``__all__``
  resolves;
- a ``database`` backup and a ``files`` backup each raise
  ``MissingExtraError(extra="database")``: the Site Descriptor is parsed by
  ``l3io-wordpress-database`` whatever the scope (A11), so the refusal comes
  after the Site Lock was taken, and the lock is released.

The subprocess prints one JSON line; the asserts are here, so a child that
crashes before printing fails rather than passes. The planted case runs the
same probe without the block and must see the extra present.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

PROBE = r"""
import json, sys
if sys.argv[1] == "blocked":
    sys.modules["l3io.wp.database"] = None
    sys.modules["opentelemetry.sdk"] = None
from structlog.testing import capture_logs
import l3io.wp.backup.api as api
from l3io.wp.backup.domain.errors import MissingExtraError

report = {"unresolved": [n for n in api.__all__ if getattr(api, n, None) is None]}
for scope in (api.BackupScope.DATABASE, api.BackupScope.FILES):
    with capture_logs() as logs:
        try:
            api.backup(sys.argv[2], scope=scope, target=sys.argv[3] + scope.value)
            outcome = "returned"
        except MissingExtraError as exc:
            outcome = "missing:" + exc.extra
        except Exception as exc:
            outcome = type(exc).__name__
    events = [e["event"] for e in logs]
    report[scope.value] = {
        "outcome": outcome,
        "locked": "site_lock_acquired" in events,
        "released": "site_lock_released" in events,
    }
print(json.dumps(report))
"""


def _site(root: Path) -> Path:
    root.mkdir()
    (root / "index.php").write_text("<?php\n", encoding="utf-8")
    (root / "wp-config.php").write_text(
        "<?php\ndefine('DB_NAME', 'wptest');\ndefine('DB_USER', 'wp');\n"
        "define('DB_PASSWORD', 'not-a-real-secret');\n"
        "define('DB_HOST', 'db.invalid');\n$table_prefix = 'wp_';\n",
        encoding="utf-8",
    )
    return root


def _probe(mode: str, tmp_path: Path) -> dict[str, Any]:
    site = _site(tmp_path / f"site-{mode}")
    env = os.environ | {"TMPDIR": str(tmp_path)}
    ran = subprocess.run(  # noqa: S603 - this interpreter, a fixed script
        [sys.executable, "-c", PROBE, mode, str(site), str(tmp_path / "out-")],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=120,
    )
    assert ran.returncode == 0, ran.stderr[-3000:]
    report: dict[str, Any] = json.loads(ran.stdout.strip().splitlines()[-1])
    return report


def test_a_base_install_imports_and_refuses_with_the_extra_named(
    tmp_path: Path,
) -> None:
    report = _probe("blocked", tmp_path)
    assert report["unresolved"] == []
    for scope in ("database", "files"):
        assert report[scope] == {
            "outcome": "missing:database",
            "locked": True,
            "released": True,
        }, scope


def test_the_probe_sees_the_extra_when_it_is_installed(tmp_path: Path) -> None:
    """The planted contrast: unblocked, the same calls do not name the extra."""
    report = _probe("open", tmp_path)
    assert report["unresolved"] == []
    assert report["files"]["outcome"] == "returned"
    assert report["database"]["outcome"] != "missing:database"
