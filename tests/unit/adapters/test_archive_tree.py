"""Owner decision 86 — the Archive directory read as a tree, and pruned folders.

``list_archives`` finds every regular ``*.tar.gz`` under the Archive
directory, flat or in subfolders, down to :data:`ARCHIVE_TREE_DEPTH` levels
(the deepest an ``archive_subdir`` template may write). It never follows a
symlinked directory, never descends into a workspace or a skipped directory
(the state directory, which holds the Safety Snapshots), and reports what it
did not enter. ``remove_emptied_directories`` removes the folders a prune
emptied, deepest first, and nothing else: never the Archive directory, a
folder still holding anything, or a symlink.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

import pytest

from l3io.wp.backup.adapters.archive_dir import (
    list_archives,
    remove_emptied_directories,
)
from l3io.wp.backup.adapters.workspace import WORKSPACE_PREFIX
from l3io.wp.backup.domain.archive_subdir import ARCHIVE_TREE_DEPTH


def _file(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not really an archive")
    return path


def _listed(root: Path, **kwargs: object) -> set[Path]:
    files, _ = list_archives(root, **kwargs)  # type: ignore[arg-type]
    return {Path(f.path) for f in files}


def test_flat_and_nested_archives_are_all_listed(tmp_path: Path) -> None:
    flat = _file(tmp_path / "wpbackup2-db-20250101000000.tar.gz")
    nested = _file(tmp_path / "shop" / "2026" / "11" / "wpbak-shop-x.tar.gz")
    _file(tmp_path / "shop" / "2026" / ".wpbak-shop-x.tar.gz.ab.partial")
    _file(tmp_path / "shop" / "notes.txt")
    assert _listed(tmp_path) == {flat, nested}


def test_the_depth_cap_is_the_templates_and_deeper_is_reported(
    tmp_path: Path,
) -> None:
    deepest = tmp_path.joinpath(*[f"d{i}" for i in range(ARCHIVE_TREE_DEPTH)])
    reached = _file(deepest / "a.tar.gz")
    _file(deepest / "too-deep" / "b.tar.gz")
    files, skipped = list_archives(tmp_path)
    assert {Path(f.path) for f in files} == {reached}
    assert [(Path(s.path), s.reason) for s in skipped] == [
        (deepest / "too-deep", f"deeper than {ARCHIVE_TREE_DEPTH} levels")
    ]


def test_a_symlinked_directory_is_never_followed(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    _file(elsewhere / "theirs.tar.gz")
    root = tmp_path / "backups"
    root.mkdir()
    (root / "link").symlink_to(elsewhere, target_is_directory=True)
    ours = _file(root / "real" / "ours.tar.gz")
    files, skipped = list_archives(root)
    assert {Path(f.path) for f in files} == {ours}
    assert [(Path(s.path), s.reason) for s in skipped] == [
        (root / "link", "a symlinked directory")
    ]


def test_workspaces_and_skipped_directories_are_not_entered(tmp_path: Path) -> None:
    _file(tmp_path / f"{WORKSPACE_PREFIX}abc" / "x.tar.gz")
    state = tmp_path / "state"
    _file(state / "snapshots" / "abc" / "snap.tar.gz")
    ours = _file(tmp_path / "site" / "ours.tar.gz")
    assert _listed(tmp_path, skip_dirs=(state,)) == {ours}
    # The skip list never hides the Archive directory itself.
    assert _listed(state, skip_dirs=(state,)) == {
        state / "snapshots" / "abc" / "snap.tar.gz"
    }


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any directory")
def test_an_unreadable_folder_is_reported_not_raised(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    _file(locked / "x.tar.gz")
    locked.chmod(0)
    try:
        files, skipped = list_archives(tmp_path)
    finally:
        locked.chmod(0o700)
    assert files == []
    assert [Path(s.path) for s in skipped] == [locked]


def test_only_the_folders_a_prune_emptied_are_removed(tmp_path: Path) -> None:
    root = tmp_path / "backups"
    gone = [
        _file(root / "shop" / "2025" / "12" / "a.tar.gz"),
        _file(root / "shop" / "2026" / "01" / "b.tar.gz"),
        _file(root / "flat.tar.gz"),
    ]
    kept = _file(root / "shop" / "2026" / "02" / "c.tar.gz")
    _file(root / "shop" / "2026" / "01" / ".x.tar.gz.y.partial")
    (root / "empty-but-untouched").mkdir()
    for path in gone:
        path.unlink()
    removed = remove_emptied_directories(root, [PurePosixPath(p) for p in gone])
    assert removed == (root / "shop" / "2025" / "12", root / "shop" / "2025")
    assert kept.exists()
    assert (root / "shop" / "2026" / "01").is_dir()  # holds a .partial
    assert (root / "empty-but-untouched").is_dir()  # not on a pruned path
    assert root.is_dir()


def test_the_archive_directory_itself_is_never_removed(tmp_path: Path) -> None:
    root = tmp_path / "backups"
    only = _file(root / "a" / "only.tar.gz")
    only.unlink()
    assert remove_emptied_directories(root, [PurePosixPath(only)]) == (root / "a",)
    assert root.is_dir()


def test_a_symlink_on_the_path_is_not_removed(tmp_path: Path) -> None:
    root = tmp_path / "backups"
    target = tmp_path / "empty"
    target.mkdir()
    root.mkdir()
    (root / "link").symlink_to(target, target_is_directory=True)
    assert remove_emptied_directories(root, [PurePosixPath(root / "link" / "x")]) == ()
    assert (root / "link").is_symlink()
    assert target.is_dir()


def test_a_path_outside_the_archive_directory_is_ignored(tmp_path: Path) -> None:
    root = tmp_path / "backups"
    root.mkdir()
    outside = tmp_path / "outside" / "x.tar.gz"
    outside.parent.mkdir()
    assert remove_emptied_directories(root, [PurePosixPath(outside)]) == ()
    assert outside.parent.is_dir()
