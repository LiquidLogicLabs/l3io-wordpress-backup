"""Owner decision 67, for real: a backup started as root runs as the site's owner.

Runs only as root (CI's containers; ``pytest.mark.root``), and drives the real
switch in a **child process**, ``python -m wpbackup2``, never in pytest's own:
a switched process cannot switch back. The passphrase is in a config file only
root can read (``0600``, root's), so a backup that switched before reading it
could not encrypt; the Site Root, the Archive directory and the state and
temporary directories belong to ``nobody``. Afterwards the Archive is
``nobody``'s, the Run's state is too, and the Run logged the switch. An
Archive directory ``nobody`` cannot write is refused before the Run opens, a
Dry Run too, and a root-owned Site Root is refused. No database is needed:
``--mode files``.
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

from tests.root_mode import ROOT_MARK, SITE_OWNER, running_as_root

pytestmark = [
    getattr(pytest.mark, ROOT_MARK),
    pytest.mark.skipif(not running_as_root(), reason="needs real root"),
]

#: The one owner every root test uses (``tests/root_mode.py``).
OWNER = SITE_OWNER
PASSPHRASE = "root-only-passphrase-67"

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


def _env(tree: Path) -> dict[str, str]:
    """The child's environment: the owner's state and workspace directories."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("L3IO_WP_BACKUP_")}
    env["XDG_STATE_HOME"] = str(_owned(tree / "owner-state", 0o700))
    env["XDG_CONFIG_HOME"] = str(_owned(tree / "owner-config", 0o700))
    env["TMPDIR"] = str(_owned(tree / "owner-tmp", 0o700))
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


def _events(stderr: str) -> list[dict[str, object]]:
    found = []
    for line in stderr.splitlines():
        try:
            found.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return found


@pytest.fixture
def tree() -> Iterator[Path]:
    """A directory the owner can traverse: pytest's own are root's ``0700``."""
    made = Path(tempfile.mkdtemp(prefix="l3io-root-backup-", dir="/tmp"))
    made.chmod(0o711)
    yield made
    shutil.rmtree(made, ignore_errors=True)


@pytest.fixture
def site(tree: Path) -> Path:
    root = _owned(tree / "site", 0o755)
    (root / "wp-content").mkdir()
    for name, text in (
        ("wp-config.php", WP_CONFIG),
        ("index.php", "<?php // site\n"),
        ("wp-content/upload.txt", "sentinel-root-backup\n"),
    ):
        (root / name).write_text(text, encoding="utf-8")
    for path in (root / "wp-content", *root.rglob("*")):
        os.chown(path, OWNER, OWNER)
    (root / "wp-config.php").chmod(0o640)
    return root


@pytest.fixture
def root_only_config(tree: Path) -> Path:
    """The passphrase, in a config file only root may read."""
    config = tree / "root-only.toml"
    config.write_text(f'passphrase = "{PASSPHRASE}"\n', encoding="utf-8")
    config.chmod(0o600)
    assert config.stat().st_uid == 0
    return config


def test_a_root_backup_runs_as_the_site_owner(
    tree: Path, site: Path, root_only_config: Path
) -> None:
    backups = _owned(tree / "backups", 0o700)
    env = _env(tree)
    done = _run(
        [
            "--config",
            str(root_only_config),
            "backup",
            "--wp-dir",
            str(site),
            "--mode",
            "files",
            "--archive-dir",
            str(backups),
            "--encrypt-with-passphrase",
        ],
        env,
        tree,
    )
    assert done.returncode == 0, done.stderr
    (switched,) = [
        e for e in _events(done.stderr) if e.get("event") == "backup_runs_as_site_owner"
    ]
    assert (switched["uid"], switched["gid"]) == (OWNER, OWNER)
    (written,) = backups.glob("wpbackup2-roottest-*.tar.gz")
    assert (written.stat().st_uid, written.stat().st_gid) == (OWNER, OWNER)
    assert PASSPHRASE not in done.stdout + done.stderr
    # The Run's state (the Site Lock, the Run Journal) is the owner's, where
    # a restore of this site, switched the same way, finds it.
    state = [p for p in Path(env["XDG_STATE_HOME"]).rglob("*") if p.exists()]
    assert state
    assert all(p.stat().st_uid == OWNER for p in state), state
    # Encrypted with the root-only passphrase: it authenticates with it.
    check = _run(
        ["verify", "--archive", str(written), "--authenticate"],
        {**env, "L3IO_WP_BACKUP_PASSPHRASE": PASSPHRASE},
        tree,
    )
    assert check.returncode == 0, check.stderr


@pytest.mark.parametrize("dry_run", [False, True], ids=["run", "dry-run"])
def test_an_archive_directory_the_owner_cannot_write_is_refused(
    tree: Path, site: Path, dry_run: bool
) -> None:
    backups = tree / "root-backups"
    backups.mkdir(mode=0o700)
    env = _env(tree)
    argv = [
        *(["--whatIf"] if dry_run else []),
        "backup",
        "--wp-dir",
        str(site),
        "--mode",
        "files",
        "--archive-dir",
        str(backups),
    ]
    done = _run(argv, env, tree)
    assert done.returncode == 3, done.stderr
    assert f"the Archive directory {str(backups)!r} is not writable" in done.stderr
    assert "ArchiveDirectoryNotWritableError" in done.stderr
    assert list(backups.iterdir()) == []
    events = [e.get("event") for e in _events(done.stderr)]
    assert "backup_runs_as_site_owner" in events
    assert "run_started" not in events


def test_a_root_owned_site_root_is_refused(tree: Path) -> None:
    site = tree / "root-site"
    site.mkdir()
    (site / "wp-config.php").write_text(WP_CONFIG, encoding="utf-8")
    backups = _owned(tree / "backups", 0o700)
    done = _run(
        [
            "backup",
            "--wp-dir",
            str(site),
            "--mode",
            "files",
            "--archive-dir",
            str(backups),
        ],
        _env(tree),
        tree,
    )
    assert done.returncode == 2, done.stderr
    assert "refused to back up as root" in done.stderr
    assert "owned by root, so there is no site owner" in done.stderr
    assert list(backups.iterdir()) == []
