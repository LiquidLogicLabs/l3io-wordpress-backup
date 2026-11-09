"""Owner decision 58, for real: a restore started as root runs as the site's owner.

Runs only as root (CI's containers; ``pytest.mark.root``), and drives the real
switch in a **child process**, ``python -m wpbackup2``, never in pytest's own:
a switched process cannot switch back. The Archive is root's (mode ``0600``),
so a restore that switched before reading it could not; the Site Root, its
state and temporary directories belong to ``nobody``. Afterwards the restored
files are ``nobody``'s and the Run logged the switch. A root-owned Site Root
is refused. No database is needed: ``--mode files``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.root_mode import ROOT_MARK, running_as_root

pytestmark = [
    getattr(pytest.mark, ROOT_MARK),
    pytest.mark.skipif(not running_as_root(), reason="needs real root"),
]

#: ``nobody`` on Debian and Ubuntu; any uid without root's rights does.
OWNER = 65534

WP_CONFIG = (
    "<?php\n"
    "define('DB_NAME', 'roottest');\n"
    "define('DB_USER', 'wp');\n"
    "define('DB_PASSWORD', 'not-a-real-secret');\n"
    "define('DB_HOST', 'localhost');\n"
    "$table_prefix = 'wp_';\n"
)


def _owned(path: Path, mode: int) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chown(path, OWNER, OWNER)
    path.chmod(mode)
    return path


def _env(tmp_path: Path) -> dict[str, str]:
    """The child's environment: the owner's state and workspace directories."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("L3IO_WP_BACKUP_")}
    env["XDG_STATE_HOME"] = str(_owned(tmp_path / "owner-state", 0o700))
    env["XDG_CONFIG_HOME"] = str(_owned(tmp_path / "owner-config", 0o700))
    env["TMPDIR"] = str(_owned(tmp_path / "owner-tmp", 0o700))
    env["HOME"] = str(_owned(tmp_path / "owner-home", 0o700))
    return env


def _run(
    argv: list[str], env: dict[str, str], cwd: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - the interpreter and fixed arguments
        [sys.executable, "-m", "wpbackup2", "--log-format", "json", *argv],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )


@pytest.fixture
def tree() -> Iterator[Path]:
    """A directory the owner can traverse: pytest's own are root's ``0700``."""
    made = Path(tempfile.mkdtemp(prefix="l3io-root-restore-", dir="/tmp"))
    made.chmod(0o711)
    yield made
    shutil.rmtree(made, ignore_errors=True)


@pytest.fixture
def archive(tree: Path) -> Path:
    """A files Archive of a source site, then made root's and readable only by it.

    The backup itself, started as root, runs as the source's owner (owner
    decision 67), so the source and the Archive directory are the owner's.
    """
    source = _owned(tree / "source", 0o755)
    for name, text in (
        ("wp-config.php", WP_CONFIG),
        ("index.php", "<?php // restored\n"),
        ("wp-content/upload.txt", "sentinel-root\n"),
    ):
        path = source / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")
    for path in (source / "wp-content", *source.rglob("*")):
        os.chown(path, OWNER, OWNER)
    backups = _owned(tree / "backups", 0o700)
    target = backups / "site.tar.gz"
    done = _run(
        [
            "backup",
            "--wp-dir",
            str(source),
            "--mode",
            "files",
            "--archive",
            str(target),
        ],
        _env(tree),
        tree,
    )
    assert done.returncode == 0, done.stderr
    os.chown(backups, 0, 0)
    os.chown(target, 0, 0)
    target.chmod(0o600)
    return target


def _events(stderr: str) -> list[dict[str, object]]:
    found = []
    for line in stderr.splitlines():
        try:
            found.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return found


def test_a_root_restore_runs_as_the_site_owner(tree: Path, archive: Path) -> None:
    site = _owned(tree / "site", 0o755)
    for name, text in (("wp-config.php", WP_CONFIG), ("index.php", "<?php // old\n")):
        (site / name).write_text(text, encoding="utf-8")
        os.chown(site / name, OWNER, OWNER)
    env = _env(tree)
    done = _run(
        [
            "restore",
            "--wp-dir",
            str(site),
            "--archive",
            str(archive),
            "--mode",
            "files",
        ],
        env,
        tree,
    )
    assert done.returncode == 0, done.stderr
    (switched,) = [
        e
        for e in _events(done.stderr)
        if e.get("event") == "restore_runs_as_site_owner"
    ]
    assert (switched["uid"], switched["gid"]) == (OWNER, OWNER)
    restored = site / "wp-content" / "upload.txt"
    assert restored.read_text(encoding="utf-8") == "sentinel-root\n"
    for path in (site / "index.php", site / "wp-content", restored):
        assert (path.stat().st_uid, path.stat().st_gid) == (OWNER, OWNER), path
    # The Run's state is the owner's: the Safety Snapshot was written there.
    snapshots = list(Path(env["XDG_STATE_HOME"]).rglob("*.tar.gz"))
    assert snapshots
    assert all(p.stat().st_uid == OWNER for p in snapshots)


def test_a_root_owned_site_root_is_refused(tree: Path, archive: Path) -> None:
    site = tree / "root-site"
    site.mkdir()
    (site / "wp-config.php").write_text(WP_CONFIG, encoding="utf-8")
    done = _run(
        [
            "restore",
            "--wp-dir",
            str(site),
            "--archive",
            str(archive),
            "--mode",
            "files",
        ],
        _env(tree),
        tree,
    )
    assert done.returncode == 2, done.stderr
    assert "owned by root, so there is no site owner" in done.stderr
    assert (site / "wp-config.php").read_text(encoding="utf-8") == WP_CONFIG
