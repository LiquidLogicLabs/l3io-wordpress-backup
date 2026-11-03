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
