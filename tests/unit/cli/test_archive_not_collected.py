"""A backup written inside its own Site Root never collects the Archive being written.

The staged file exists before the Site Root is collected, so an Archive target
inside the Site Root (the default is the current directory) must be excluded
from the collection, or the Archive swallows its own partial file.
"""

from __future__ import annotations

import tarfile
from pathlib import Path, PurePosixPath

import pytest

pytest.importorskip("l3io.wp.database")

from l3io.wp.backup.cli import main
from l3io.wp.backup.cli.backup import _SkipStaged
from l3io.wp.backup.domain.archive_format import PAYLOAD_MEMBER
from l3io.wp.backup.domain.exclusion import INCLUDE_EVERYTHING


def test_skip_staged_excludes_only_the_staged_file(tmp_path: Path) -> None:
    root = tmp_path / "www"
    staged = root / ".out.tar.gz.abc.partial"
    policy = _SkipStaged(INCLUDE_EVERYTHING, root, staged)
    assert policy.decide(
        PurePosixPath(".out.tar.gz.abc.partial"), is_dir=False
    ).excluded
    assert not policy.decide(PurePosixPath("index.php"), is_dir=False).excluded
    assert not policy.decide(
        PurePosixPath(".out.tar.gz.abc.partial"), is_dir=True
    ).excluded


def test_a_files_backup_inside_the_site_root_does_not_contain_its_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = tmp_path / "www"
    site.mkdir()
    (site / "wp-config.php").write_text(
        "<?php\ndefine('DB_NAME','x');\ndefine('DB_USER','u');\n"
        "define('DB_PASSWORD','p');\ndefine('DB_HOST','h');\n$table_prefix='wp_';\n",
        encoding="utf-8",
    )
    (site / "index.php").write_text("<?php\n", encoding="utf-8")
    archive = site / "out.tar.gz"
    code = main(
        [
            "backup",
            "--wp-dir",
            str(site),
            "--archive",
            str(archive),
            "--mode",
            "files",
            # BL-E002-064: inside the Site Root only when allowed.
            "--allow-target-in-site-root",
        ]
    )
    assert code == 0
    with tarfile.open(archive) as outer:
        member = outer.extractfile(PAYLOAD_MEMBER)
        assert member is not None
        with tarfile.open(fileobj=member, mode="r|*") as inner:
            names = [entry.name for entry in inner]
    assert any(name.endswith("index.php") for name in names)
    assert not any(name.endswith(".partial") for name in names)


# --- BL-E002-100 (red team RT-1): earlier Archives are not collected ----------


def test_the_default_name_and_its_staged_file_match_the_skipped_globs(
    tmp_path: Path,
) -> None:
    """The globs are checked against what the name and the store produce."""
    import fnmatch
    from datetime import UTC, datetime

    from l3io.wp.backup.adapters.store import LocalArchiveStore
    from l3io.wp.backup.cli.backup import _OWN_ARCHIVE_GLOBS
    from l3io.wp.backup.domain.storage_target import (
        StorageTarget,
        default_archive_name,
    )

    name = default_archive_name("db", datetime(2026, 11, 7, tzinfo=UTC))
    assert fnmatch.fnmatchcase(name, _OWN_ARCHIVE_GLOBS[0])
    staged = LocalArchiveStore(verify=lambda _path: None).stage(
        StorageTarget(scheme="file", path=PurePosixPath(tmp_path / name))
    )
    try:
        assert staged.staged_path is not None
        assert fnmatch.fnmatchcase(staged.staged_path.name, _OWN_ARCHIVE_GLOBS[1])
    finally:
        staged.discard()


def test_skip_staged_leaves_out_earlier_archives_even_with_no_exclusions(
    tmp_path: Path,
) -> None:
    root = tmp_path / "www"
    policy = _SkipStaged(INCLUDE_EVERYTHING, root, root / ".x.tar.gz.a.partial")
    for name in (
        "wpbackup2-db-20261103184135.tar.gz",
        "sub/wpbackup2-db-20261103184135.tar.gz",
        ".wpbackup2-db-20261103184135.tar.gz.l9cp4mzz.partial",
    ):
        assert policy.decide(PurePosixPath(name), is_dir=False).excluded, name
    for name in ("wp-content/uploads/photo.tar.gz", "wpbackup2-notes.txt"):
        assert not policy.decide(PurePosixPath(name), is_dir=False).excluded, name


def test_a_second_default_backup_from_the_site_root_does_not_hold_the_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The red team's reproduction: ``cd site && backup --wp-dir .`` twice."""
    site = tmp_path / "www"
    site.mkdir()
    (site / "wp-config.php").write_text(
        "<?php\ndefine('DB_NAME','rtdb');\ndefine('DB_USER','u');\n"
        "define('DB_PASSWORD','p');\ndefine('DB_HOST','h');\n$table_prefix='wp_';\n",
        encoding="utf-8",
    )
    (site / "index.php").write_text("<?php\n", encoding="utf-8")
    monkeypatch.chdir(site)
    earlier = site / "wpbackup2-rtdb-20261103184135.tar.gz"
    earlier.write_bytes(b"an earlier archive")
    leftover = site / ".wpbackup2-rtdb-20261103184136.tar.gz.l9cp4mzz.partial"
    leftover.write_bytes(b"a killed run's staged file")
    argv = ["backup", "--wp-dir", ".", "--mode", "files", "--allow-target-in-site-root"]
    assert main(argv) == 0
    written = sorted(set(site.glob("wpbackup2-rtdb-*.tar.gz")) - {earlier})
    assert len(written) == 1
    with tarfile.open(written[0]) as outer:
        member = outer.extractfile(PAYLOAD_MEMBER)
        assert member is not None
        with tarfile.open(fileobj=member, mode="r|*") as inner:
            names = [entry.name for entry in inner]
    assert any(name.endswith("index.php") for name in names)
    assert not any(name.endswith((earlier.name, leftover.name)) for name in names)
