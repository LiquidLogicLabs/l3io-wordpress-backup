"""AD-8, owner decision 76 — ``l3io.wp.backup.api`` in a base and a broken install.

Since 1.0.0 the family is a required dependency, so a base install has
``l3io.wp.database`` and lacks only the optional extras (``otel``; ``aws``'s
boto3 is not imported until an AWS secret is resolved). A subprocess blocks
modules in ``sys.modules`` before the first import, which is what an
environment without them looks like to an ``import``:

- ``base`` blocks ``opentelemetry.sdk`` only: ``import l3io.wp.backup.api``
  succeeds, every name in ``__all__`` resolves, and neither backup is refused
  for a missing extra or an incomplete install;
- ``broken`` also blocks ``l3io.wp.database``, as an install made with
  ``--no-deps`` would be: the API still imports, and a ``database`` and a
  ``files`` backup each raise ``IncompleteInstallError`` naming
  ``l3io-wordpress-database`` -- never an extra to install -- after the Site
  Lock was taken (the Site Descriptor is parsed by database whatever the
  scope, A11), and the lock is released.

The subprocess prints one JSON line; the asserts are here, so a child that
crashes before printing fails rather than passes.

The site's ``DB_PASSWORD`` is a unique sentinel per probe, and the child's
stdout and stderr are checked not to contain it (BL-E006-003).

As root (CI), the child is a library host, so it drops to the Site's owner
before the backups (owner decision 68; ``tests/root_mode.py``).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from tests.root_mode import give_to_site_owner

PROJECT_ROOT = Path(__file__).resolve().parents[2]

PROBE = r"""
import json, sys
sys.modules["opentelemetry.sdk"] = None
if sys.argv[1] == "broken":
    sys.modules["l3io.wp.database"] = None
from tests.root_mode import become_site_owner
become_site_owner()
from structlog.testing import capture_logs
import l3io.wp.backup.api as api
from l3io.wp.backup.domain.errors import IncompleteInstallError, MissingExtraError

report = {"unresolved": [n for n in api.__all__ if getattr(api, n, None) is None]}
for scope in (api.BackupScope.DATABASE, api.BackupScope.FILES):
    with capture_logs() as logs:
        try:
            api.backup(sys.argv[2], scope=scope, target=sys.argv[3] + scope.value)
            outcome = "returned"
        except IncompleteInstallError as exc:
            outcome = "incomplete:" + exc.distribution
            assert "extra" not in str(exc), str(exc)
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


def _site(root: Path, password: str) -> Path:
    root.mkdir()
    (root / "index.php").write_text("<?php\n", encoding="utf-8")
    (root / "wp-config.php").write_text(
        "<?php\ndefine('DB_NAME', 'wptest');\ndefine('DB_USER', 'wp');\n"
        f"define('DB_PASSWORD', '{password}');\n"
        "define('DB_HOST', 'db.invalid');\n$table_prefix = 'wp_';\n",
        encoding="utf-8",
    )
    return root


def _probe(mode: str, tmp_path: Path) -> dict[str, Any]:
    password = f"base-pw-{uuid.uuid4().hex}"
    site = _site(tmp_path / f"site-{mode}", password)
    env = os.environ | {"TMPDIR": str(tmp_path)}
    give_to_site_owner(tmp_path, environ=env)
    ran = subprocess.run(  # noqa: S603 - this interpreter, a fixed script
        [sys.executable, "-c", PROBE, mode, str(site), str(tmp_path / "out-")],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=120,
    )
    leaked = password in ran.stdout or password in ran.stderr
    assert not leaked, "the site's DB_PASSWORD reached the probe's output"
    assert ran.returncode == 0, ran.stderr[-3000:]
    report: dict[str, Any] = json.loads(ran.stdout.strip().splitlines()[-1])
    return report


def test_a_broken_install_imports_and_says_it_is_incomplete(
    tmp_path: Path,
) -> None:
    report = _probe("broken", tmp_path)
    assert report["unresolved"] == []
    for scope in ("database", "files"):
        assert report[scope] == {
            "outcome": "incomplete:l3io-wordpress-database",
            "locked": True,
            "released": True,
        }, scope


def test_a_base_install_needs_no_extra_to_back_up(tmp_path: Path) -> None:
    """The contrast: only the optional extras absent, nothing is refused for it.

    The ``database`` backup still fails (``db.invalid`` cannot be reached), but
    not as a missing extra or an incomplete install.
    """
    report = _probe("base", tmp_path)
    assert report["unresolved"] == []
    assert report["files"]["outcome"] == "returned"
    outcome = report["database"]["outcome"]
    assert not outcome.startswith(("missing:", "incomplete:")), outcome
