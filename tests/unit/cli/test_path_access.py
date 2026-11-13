"""Owner decision 87 — a path the Run's user cannot use is refused, never fixed.

Each command checks, before its Run opens and a Dry Run too, the paths it
needs as the user it runs as, and refuses one that user cannot read or write
with ``PathNotAccessibleError`` (exit 3): the message names the path, the
effective ``uid:gid``, the owner, group and mode found, the fix, and that
wpbackup2 never changes ownership. Nothing is changed: no mode or owner is
touched and no Run is opened (no lock, no journal).

The checks are made with the real mode bits, so they are skipped as root
(root reads and writes whatever the bits say).
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from l3io.wp.backup.adapters.access import archive_open_error, require_access
from l3io.wp.backup.cli import main
from l3io.wp.backup.domain.errors import ArchiveNotFoundError, PathNotAccessibleError
from l3io.wp.backup.domain.exit_codes import ExitCode

pytestmark = pytest.mark.skipif(
    os.geteuid() == 0, reason="root reads and writes whatever the mode bits"
)

ME = f"{os.geteuid()}:{os.getegid()}"


@pytest.fixture
def locked_away() -> Iterator[list[Path]]:
    """Paths a test made unusable; their modes are restored for cleanup."""
    paths: list[Path] = []
    yield paths
    for path in paths:
        path.chmod(0o700)


def _deny(path: Path, mode: int, locked_away: list[Path]) -> tuple[int, int, int]:
    path.chmod(mode)
    locked_away.append(path)
    info = path.stat()
    return info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)


def _failure(capsys: pytest.CaptureFixture[str]) -> str:
    return capsys.readouterr().err


def _assert_refusal(err: str, path: Path, access: str) -> None:
    verb = "readable" if access == "read" else "writable"
    assert "PathNotAccessibleError" in err or "is not " + verb in err
    assert repr(str(path)) in err
    assert f"is not {verb} by uid {ME}" in err
    assert "wpbackup2 never changes ownership" in err
    assert "nothing was changed" in err


def _tree_state(root: Path) -> list[tuple[str, int, int, int]]:
    """Every path under ``root`` with its owner, group and mode."""
    found = []
    for path in sorted([root, *root.rglob("*")]):
        info = path.lstat()
        found.append((str(path), info.st_uid, info.st_gid, info.st_mode))
    return found


@pytest.mark.parametrize("dry_run", [False, True], ids=["run", "dry-run"])
def test_a_backup_refuses_a_state_directory_it_cannot_write(
    site: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    locked_away: list[Path],
    dry_run: bool,
) -> None:
    state = Path(os.environ["XDG_STATE_HOME"])  # tests/conftest.py's, per test
    (state / "l3io-wp-backup").mkdir(mode=0o700)
    _deny(state / "l3io-wp-backup", 0o500, locked_away)
    before = _tree_state(state)
    archives = tmp_path / "archives"
    archives.mkdir()
    argv = ["--whatIf"] if dry_run else []
    argv += ["backup", "--wp-dir", str(site), "--mode", "files"]
    argv += ["--archive-dir", str(archives)]
    assert main(argv) == ExitCode.REFUSED
    err = _failure(capsys)
    _assert_refusal(err, state / "l3io-wp-backup", "write")
    assert "the state directory" in err
    assert "mode 0500" in err
    assert _tree_state(state) == before
    assert list(archives.iterdir()) == []


def test_a_backup_refuses_an_archive_directory_it_cannot_write(
    site: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    locked_away: list[Path],
) -> None:
    archives = tmp_path / "archives"
    archives.mkdir()
    uid, gid, _ = _deny(archives, 0o555, locked_away)
    argv = ["backup", "--wp-dir", str(site), "--mode", "files"]
    assert main([*argv, "--archive-dir", str(archives)]) == ExitCode.REFUSED
    err = _failure(capsys)
    _assert_refusal(err, archives, "write")
    assert f"owned by {uid}:{gid}, mode 0555" in err
    assert "the Archive directory" in err


def test_a_missing_archive_directory_is_checked_where_it_would_be_made(
    site: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    locked_away: list[Path],
) -> None:
    parent = tmp_path / "mnt"
    parent.mkdir()
    _deny(parent, 0o555, locked_away)
    wanted = parent / "backups" / "site"
    argv = ["backup", "--wp-dir", str(site), "--mode", "files"]
    assert main([*argv, "--archive-dir", str(wanted)]) == ExitCode.REFUSED
    err = _failure(capsys)
    _assert_refusal(err, wanted, "write")
    assert f"checked at {str(parent)!r}" in err


def test_a_backup_refuses_a_tmpdir_it_cannot_write(
    site: Path,
    tmp_path: Path,
    workdir: Path,
    capsys: pytest.CaptureFixture[str],
    locked_away: list[Path],
) -> None:
    """Python would fall back to another temporary directory without a word."""
    _deny(workdir, 0o555, locked_away)
    archives = tmp_path / "archives"
    archives.mkdir()
    argv = ["backup", "--wp-dir", str(site), "--mode", "files"]
    assert main([*argv, "--archive-dir", str(archives)]) == ExitCode.REFUSED
    err = _failure(capsys)
    _assert_refusal(err, workdir, "write")
    assert "temporary directory (TMPDIR)" in err


def test_a_backup_refuses_a_site_root_it_cannot_read(
    site: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    locked_away: list[Path],
) -> None:
    _deny(site, 0o300, locked_away)
    archives = tmp_path / "archives"
    archives.mkdir()
    argv = ["backup", "--wp-dir", str(site), "--mode", "files"]
    assert main([*argv, "--archive-dir", str(archives)]) == ExitCode.REFUSED
    err = _failure(capsys)
    _assert_refusal(err, site, "read")
    assert "the Site Root" in err


def test_a_prune_refuses_an_archive_directory_it_cannot_write(
    site: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    locked_away: list[Path],
) -> None:
    archives = tmp_path / "archives"
    archives.mkdir()
    _deny(archives, 0o555, locked_away)
    argv = ["prune", "--keep", "1", "--wp-dir", str(site)]
    assert main([*argv, "--archive-dir", str(archives)]) == ExitCode.REFUSED
    _assert_refusal(_failure(capsys), archives, "write")


@pytest.mark.parametrize("command", ["verify", "restore"])
def test_an_archive_it_cannot_read_is_refused_not_reported_missing(
    site: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    locked_away: list[Path],
    command: str,
) -> None:
    archive = tmp_path / "site.tar.gz"
    archive.write_bytes(b"not read")
    _deny(archive, 0o000, locked_away)
    argv = [command, "--archive", str(archive)]
    if command == "restore":
        argv += ["--wp-dir", str(site), "--mode", "files"]
    assert main(argv) == ExitCode.REFUSED
    err = _failure(capsys)
    _assert_refusal(err, archive, "read")
    assert "does not exist" not in err


def test_a_restore_refuses_a_snapshot_directory_it_cannot_write(
    site: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    locked_away: list[Path],
) -> None:
    archive = tmp_path / "site.tar.gz"
    argv = ["backup", "--wp-dir", str(site), "--mode", "files", "--archive"]
    assert main([*argv, str(archive)]) == 0
    capsys.readouterr()
    snapshots = tmp_path / "snapshots"
    snapshots.mkdir()
    _deny(snapshots, 0o555, locked_away)
    argv = ["restore", "--archive", str(archive), "--wp-dir", str(site)]
    argv += ["--mode", "files", "--snapshot-dir", str(snapshots)]
    assert main(argv) == ExitCode.REFUSED
    _assert_refusal(_failure(capsys), snapshots, "write")


# ---- the check itself ---------------------------------------------------------


def test_a_usable_path_and_a_missing_one_to_read_pass(tmp_path: Path) -> None:
    require_access(tmp_path, role="x", access="write")
    require_access(tmp_path / "not" / "made", role="x", access="write")
    require_access(tmp_path / "absent", role="x", access="read")


def test_a_folder_above_it_cannot_enter_is_the_one_named(
    tmp_path: Path, locked_away: list[Path]
) -> None:
    outer = tmp_path / "outer"
    (outer / "inner").mkdir(parents=True)
    _deny(outer, 0o600, locked_away)
    with pytest.raises(PathNotAccessibleError) as caught:
        require_access(outer / "inner" / "a.tar.gz", role="Archive", access="read")
    assert caught.value.checked == str(outer)
    assert caught.value.mode == 0o600
    assert caught.value.access == "read"


def test_the_fix_offers_running_as_another_owner_but_never_as_root() -> None:
    other = PathNotAccessibleError(
        path="/backups",
        role="Archive directory",
        access="write",
        uid=12345,
        gid=12345,
        checked="/backups",
        owner_uid=33,
        owner_gid=33,
        mode=0o700,
    )
    assert "chown -R 12345:12345" in str(other)
    assert "PUID=33 PGID=33, or --user 33:33" in str(other)
    root = PathNotAccessibleError(
        path="/backups",
        role="Archive directory",
        access="write",
        uid=33,
        gid=33,
        checked="/backups",
        owner_uid=0,
        owner_gid=0,
        mode=0o755,
    )
    assert "PUID=0" not in str(root)
    assert "owned by 0:0, mode 0755" in str(root)


def test_a_missing_archive_is_still_not_found(tmp_path: Path) -> None:
    missing = tmp_path / "absent.tar.gz"
    with pytest.raises(FileNotFoundError) as caught:
        missing.open("rb")
    assert isinstance(archive_open_error(missing, caught.value), ArchiveNotFoundError)
