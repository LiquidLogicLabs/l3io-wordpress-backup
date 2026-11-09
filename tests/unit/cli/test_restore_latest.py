"""Owner decision 64 — ``restore --archive latest``, through the CLI.

Real Archives written by ``backup`` into one directory, beside another site's
newer Archive and a file that is no Archive: ``latest`` names this site's,
logs the choice and shows it in the Dry Run; every spelling works (flag,
variable, config key); a site with none, or a target with no
``wp-config.php`` to tell, is refused (``USAGE``). ``read_leading_manifest``
reads the Manifest without the rest of the Archive.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from l3io.wp.backup.adapters.tar import read_leading_manifest
from l3io.wp.backup.cli import main
from l3io.wp.backup.domain.exit_codes import ExitCode
from tests.conftest import pipeline_events


def _site(root: Path, db_name: str) -> Path:
    root.mkdir()
    (root / "wp-config.php").write_text(
        "<?php\n"
        f"define('DB_NAME', '{db_name}');\n"
        "define('DB_USER', 'wp');\n"
        "define('DB_PASSWORD', 'not-a-real-secret');\n"
        "define('DB_HOST', 'localhost');\n"
        "$table_prefix = 'wp_';\n",
        encoding="utf-8",
    )
    (root / "index.php").write_text("<?php\n", encoding="utf-8")
    return root


def _backup(site: Path, backups: Path) -> Path:
    before = set(backups.iterdir())
    argv = ["backup", "--wp-dir", str(site), "--mode", "files"]
    assert main([*argv, "--archive-dir", str(backups)]) == 0
    (written,) = set(backups.iterdir()) - before
    return written


@pytest.fixture
def layout(tmp_path: Path, workdir: Path) -> tuple[Path, Path, Path]:
    """This site's Archive, then another site's newer one, and a stray file."""
    backups = tmp_path / "backups"
    backups.mkdir()
    site = _site(tmp_path / "site", "wptest")
    mine = _backup(site, backups)
    other = _site(tmp_path / "other", "otherdb")
    theirs = _backup(other, backups)
    # Newer by the file system too: the choice is the Manifest's, not mtime's.
    shutil.copystat(mine, theirs)
    (backups / "notes.tar.gz").write_text("not an archive\n", encoding="utf-8")
    return site, backups, mine


def _restore(site: Path, *extra: str) -> list[str]:
    return [
        "--log-format",
        "console",
        "--whatIf",
        "restore",
        "--wp-dir",
        str(site),
        "--mode",
        "files",
        *extra,
    ]


def test_latest_names_this_sites_newest_archive_and_shows_it(
    layout: tuple[Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    site, backups, mine = layout
    argv = _restore(site, "--archive", "latest", "--archive-dir", str(backups))
    assert main(argv) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith(f"archive latest: {mine} (created ")
    (chosen,) = [
        e for e in pipeline_events(caplog) if e["event"] == "archive_latest_selected"
    ]
    assert chosen["archive"] == str(mine)
    assert (chosen["considered"], chosen["matched"]) == (3, 1)
    assert chosen["archive_dir"] == str(backups)


@pytest.mark.parametrize("spelling", ["variable", "config"])
def test_latest_and_the_directory_from_a_variable_or_the_config_file(
    layout: tuple[Path, Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    spelling: str,
) -> None:
    site, backups, mine = layout
    argv = _restore(site)
    if spelling == "variable":
        monkeypatch.setenv("L3IO_WP_BACKUP_ARCHIVE", "latest")
        monkeypatch.setenv("L3IO_WP_BACKUP_ARCHIVE_DIR", str(backups))
    else:
        config = tmp_path / "config.toml"
        config.write_text(
            f'archive = "latest"\narchive_dir = "{backups}"\n', encoding="utf-8"
        )
        argv = ["--config", str(config), *argv]
    assert main(argv) == 0
    assert f"archive latest: {mine}" in capsys.readouterr().out


def test_the_default_directory_is_the_backups_default(
    layout: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    site, backups, mine = layout
    monkeypatch.chdir(backups)
    assert main(_restore(site, "--archive", "latest")) == 0
    assert f"archive latest: {mine}" in capsys.readouterr().out


def test_a_site_with_no_archive_there_is_a_usage_error(
    layout: tuple[Path, Path, Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, backups, _ = layout
    lonely = _site(tmp_path / "lonely", "lonelydb")
    argv = _restore(lonely, "--archive", "latest", "--archive-dir", str(backups))
    assert main(argv) == int(ExitCode.USAGE)
    err = capsys.readouterr().err
    assert "none of the 3 archives read" in err
    assert "database lonelydb" in err


def test_a_target_without_wp_config_cannot_tell_its_archives(
    layout: tuple[Path, Path, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, backups, _ = layout
    bare = tmp_path / "bare"
    bare.mkdir()
    argv = _restore(bare, "--archive", "latest", "--archive-dir", str(backups))
    assert main(argv) == int(ExitCode.USAGE)
    assert "needs the target's wp-config.php" in capsys.readouterr().err


def test_the_manifest_is_read_without_the_rest_of_the_archive(
    tmp_path: Path, workdir: Path
) -> None:
    import os

    site = _site(tmp_path / "big", "bigdb")
    (site / "blob.bin").write_bytes(os.urandom(4 * 1024 * 1024))
    backups = tmp_path / "big-backups"
    backups.mkdir()
    archive = _backup(site, backups)
    whole = read_leading_manifest(archive)
    assert whole is not None
    assert '"db_name": "bigdb"' in whole
    cut = tmp_path / "cut.tar.gz"
    with archive.open("rb") as handle:
        # Past the Manifest, a quarter of the payload: the rest is never read.
        cut.write_bytes(handle.read(1024 * 1024))
    assert read_leading_manifest(cut) == whole
    junk = tmp_path / "notes.tar.gz"
    junk.write_text("not an archive\n", encoding="utf-8")
    assert read_leading_manifest(junk) is None
    assert read_leading_manifest(tmp_path / "missing.tar.gz") is None
