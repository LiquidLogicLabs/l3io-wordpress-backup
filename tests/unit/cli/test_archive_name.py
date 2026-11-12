"""Owner decision 82 — the default Archive name, ``wpbak-<site>-<UTC stamp>``.

``<site>`` is the ``site_name`` setting in each of its three spellings
(``--site-name``, ``L3IO_WP_BACKUP_SITE_NAME``, ``site_name`` in the config
file), else the database name, kept to ``[A-Za-z0-9._-]``; a value with
nothing left is a usage error before any Run, from every spelling. Archives
under the new and the legacy ``wpbackup2-*`` name are both left out of a
backup of a Site Root that holds them, and ``restore --archive latest``,
which matches by Manifest, picks the newest of a site's Archives across both
names and never another site's.
"""

from __future__ import annotations

import re
import tarfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from l3io.wp.backup.adapters.clock import SystemClock
from l3io.wp.backup.cli import main
from l3io.wp.backup.domain.archive_format import PAYLOAD_MEMBER
from l3io.wp.backup.domain.exit_codes import ExitCode
from tests.conftest import pipeline_events
from tests.unit.cli.conftest import FakeDatabase

_STAMP = r"\d{8}T\d{6}Z"


def _written(backups: Path) -> Path:
    (written,) = backups.glob("*.tar.gz")
    return written


@pytest.mark.parametrize("spelling", ["flag", "variable", "config"])
def test_the_site_name_setting_names_the_archive(
    site: Path,
    tmp_path: Path,
    fake_database: FakeDatabase,
    monkeypatch: pytest.MonkeyPatch,
    spelling: str,
) -> None:
    backups = tmp_path / "backups"
    backups.mkdir()
    argv = ["backup", "--wp-dir", str(site), "--archive-dir", str(backups)]
    if spelling == "flag":
        argv += ["--site-name", "Example Shop"]
    elif spelling == "variable":
        monkeypatch.setenv("L3IO_WP_BACKUP_SITE_NAME", "Example Shop")
    else:
        config = tmp_path / "config.toml"
        config.write_text('site_name = "Example Shop"\n', encoding="utf-8")
        argv = ["--config", str(config), *argv]
    assert main(argv) == 0
    assert re.fullmatch(
        rf"wpbak-Example-Shop-{_STAMP}\.tar\.gz", _written(backups).name
    )


def test_without_a_site_name_the_database_name_is_used(
    site: Path, tmp_path: Path, fake_database: FakeDatabase
) -> None:
    backups = tmp_path / "backups"
    backups.mkdir()
    assert main(["backup", "--wp-dir", str(site), "--archive-dir", str(backups)]) == 0
    assert re.fullmatch(rf"wpbak-wptest-{_STAMP}\.tar\.gz", _written(backups).name)


@pytest.mark.parametrize("spelling", ["flag", "variable", "config"])
def test_a_site_name_with_nothing_usable_is_a_usage_error(
    site: Path,
    tmp_path: Path,
    fake_database: FakeDatabase,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    spelling: str,
) -> None:
    backups = tmp_path / "backups"
    backups.mkdir()
    argv = ["backup", "--wp-dir", str(site), "--archive-dir", str(backups)]
    if spelling == "flag":
        argv += ["--site-name", "!!!"]
    elif spelling == "variable":
        monkeypatch.setenv("L3IO_WP_BACKUP_SITE_NAME", "!!!")
    else:
        config = tmp_path / "config.toml"
        config.write_text('site_name = "!!!"\n', encoding="utf-8")
        argv = ["--config", str(config), *argv]
    with pytest.raises(SystemExit) as caught:
        main(argv)
    assert caught.value.code == ExitCode.USAGE
    err = capsys.readouterr().err
    if spelling == "flag":
        assert "no character usable in an archive name" in err
    else:
        where = "L3IO_WP_BACKUP_SITE_NAME" if spelling == "variable" else "site_name"
        assert where in err
    assert "--site-name" in err
    assert list(backups.iterdir()) == []


def test_a_database_name_with_nothing_usable_is_a_usage_error(
    tmp_path: Path, workdir: Path, fake_database: FakeDatabase
) -> None:
    del workdir
    site = tmp_path / "odd"
    site.mkdir()
    (site / "wp-config.php").write_text(
        "<?php\ndefine('DB_NAME', '$$$');\ndefine('DB_USER', 'wp');\n"
        "define('DB_PASSWORD', 'not-a-real-secret');\n"
        "define('DB_HOST', 'localhost');\n$table_prefix = 'wp_';\n",
        encoding="utf-8",
    )
    backups = tmp_path / "backups"
    backups.mkdir()
    argv = ["backup", "--wp-dir", str(site), "--archive-dir", str(backups)]
    assert main(argv) == ExitCode.USAGE
    assert list(backups.iterdir()) == []


def test_new_and_legacy_default_archives_in_the_site_root_are_not_collected(
    tmp_path: Path, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del workdir
    site = tmp_path / "www"
    site.mkdir()
    (site / "wp-config.php").write_text(
        "<?php\ndefine('DB_NAME','rtdb');\ndefine('DB_USER','u');\n"
        "define('DB_PASSWORD','p');\ndefine('DB_HOST','h');\n$table_prefix='wp_';\n",
        encoding="utf-8",
    )
    (site / "index.php").write_text("<?php\n", encoding="utf-8")
    earlier = [
        site / "wpbackup2-rtdb-20261103184135.tar.gz",
        site / "wpbak-rtdb-20261104T000000Z.tar.gz",
        site / ".wpbak-rtdb-20261104T000001Z.tar.gz.l9cp4mzz.partial",
    ]
    for path in earlier:
        path.write_bytes(b"an earlier archive")
    monkeypatch.chdir(site)
    argv = ["backup", "--wp-dir", ".", "--mode", "files", "--archive-dir", "."]
    assert main(argv) == 0
    (written,) = set(site.glob("wpbak-rtdb-*.tar.gz")) - set(earlier)
    with tarfile.open(written) as outer:
        member = outer.extractfile(PAYLOAD_MEMBER)
        assert member is not None
        with tarfile.open(fileobj=member, mode="r|*") as inner:
            names = [entry.name for entry in inner]
    assert any(name.endswith("index.php") for name in names)
    assert not any(name.endswith(tuple(p.name for p in earlier)) for name in names)


def _site(root: Path, db_name: str) -> Path:
    root.mkdir()
    (root / "wp-config.php").write_text(
        f"<?php\ndefine('DB_NAME', '{db_name}');\ndefine('DB_USER', 'wp');\n"
        "define('DB_PASSWORD', 'not-a-real-secret');\n"
        "define('DB_HOST', 'localhost');\n$table_prefix = 'wp_';\n",
        encoding="utf-8",
    )
    (root / "index.php").write_text("<?php\n", encoding="utf-8")
    return root


def _backup(site: Path, backups: Path, *extra: str) -> Path:
    before = set(backups.iterdir())
    argv = ["backup", "--wp-dir", str(site), "--mode", "files", *extra]
    assert main([*argv, "--archive-dir", str(backups)]) == 0
    (written,) = set(backups.iterdir()) - before
    return written


def test_latest_picks_the_newest_across_legacy_and_new_names(
    tmp_path: Path,
    workdir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A ``wpbackup2-*`` Archive, a newer ``wpbak-*`` one of the same site,
    and another site's ``wpbak-*`` Archive, newest of all: the site's newer
    one is chosen, whatever the names sort to."""
    del workdir
    now = [datetime(2026, 11, 3, 18, 41, 35, tzinfo=UTC)]
    monkeypatch.setattr(SystemClock, "now", lambda _self: now[0])
    backups = tmp_path / "backups"
    backups.mkdir()
    site = _site(tmp_path / "site", "wptest")
    first = _backup(site, backups)
    legacy = first.with_name("wpbackup2-wptest-20261103184135.tar.gz")
    first.rename(legacy)
    now[0] = datetime(2026, 11, 11, 3, 0, 0, tzinfo=UTC)
    newer = _backup(site, backups, "--site-name", "aaa")  # sorts before the legacy
    now[0] = datetime(2026, 11, 12, 3, 0, 0, tzinfo=UTC)
    other = _site(tmp_path / "other", "otherdb")
    theirs = _backup(other, backups, "--site-name", "wptest")
    assert newer.name.startswith("wpbak-aaa-")
    assert theirs.name.startswith("wpbak-wptest-")
    capsys.readouterr()
    argv = [
        "--log-format",
        "console",
        "--whatIf",
        "restore",
        "--wp-dir",
        str(site),
        "--mode",
        "files",
        "--archive",
        "latest",
        "--archive-dir",
        str(backups),
    ]
    assert main(argv) == 0
    (chosen,) = [
        e for e in pipeline_events(caplog) if e["event"] == "archive_latest_selected"
    ]
    assert chosen["archive"] == str(newer)
    assert (chosen["considered"], chosen["matched"]) == (3, 2)
