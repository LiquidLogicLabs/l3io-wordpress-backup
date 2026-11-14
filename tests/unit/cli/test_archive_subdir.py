"""Owner decision 86 — optional Archive subfolders, end to end through the CLI.

``archive_subdir`` (``--archive-subdir``, ``L3IO_WP_BACKUP_ARCHIVE_SUBDIR``,
the ``archive_subdir`` key) puts the default-named Archive in a subfolder of
the Archive directory, made ``0700``; unset, the Archive directory stays flat.
A bad template is a usage error from every spelling, before any Run, and a
subfolder that is a symlink, or a site name that would climb out, is refused
before anything is written. ``--archive`` still names the file outright. The
Dry Run shows the resolved path and creates nothing.

``restore --archive latest`` and ``prune`` read the whole tree, so flat and
nested Archives, under today's and the legacy names, are one site's set;
another site's never is. A prune removes the folders it emptied
(``archive_dir_removed``) and nothing else, and the stale ``.partial`` sweep
reaches every subfolder.
"""

from __future__ import annotations

import os
import stat
import tarfile
import time
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import pytest

from l3io.wp.backup import api
from l3io.wp.backup.adapters.clock import SystemClock
from l3io.wp.backup.adapters.workspace import STALE_AFTER_SECONDS
from l3io.wp.backup.cli import main
from l3io.wp.backup.domain.archive_format import PAYLOAD_MEMBER
from l3io.wp.backup.domain.exit_codes import ExitCode
from tests.conftest import pipeline_events

TEMPLATE = "{site}/{yyyy}/{mm}"
SECRET = "subdir-test-passphrase-7f3a"


class Clock:
    """``SystemClock.now`` pinned to :attr:`now`."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.now = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
        monkeypatch.setattr(SystemClock, "now", lambda _self: self.now)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    return Clock(monkeypatch)


@pytest.fixture
def backups(tmp_path: Path) -> Path:
    path = tmp_path / "backups"
    path.mkdir()
    return path


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


def _backup_argv(site: Path, backups: Path, *extra: str) -> list[str]:
    return [
        "backup",
        "--wp-dir",
        str(site),
        "--mode",
        "files",
        "--archive-dir",
        str(backups),
        *extra,
    ]


def _tree(root: Path) -> set[Path]:
    return set(root.rglob("*"))


# --------------------------------------------------------------------------
# The setting: three spellings, and every refusal from each
# --------------------------------------------------------------------------


@pytest.mark.parametrize("spelling", ["flag", "variable", "config"])
def test_each_spelling_writes_the_archive_into_the_subfolder(
    site: Path,
    backups: Path,
    clock: Clock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    spelling: str,
) -> None:
    argv = _backup_argv(site, backups)
    if spelling == "flag":
        argv += ["--archive-subdir", TEMPLATE]
    elif spelling == "variable":
        monkeypatch.setenv("L3IO_WP_BACKUP_ARCHIVE_SUBDIR", TEMPLATE)
    else:
        config = tmp_path / "config.toml"
        config.write_text(f'archive_subdir = "{TEMPLATE}"\n', encoding="utf-8")
        argv = ["--config", str(config), *argv]
    capsys.readouterr()
    assert main(argv) == 0
    expected = (
        backups / "wptest" / "2026" / "11" / "wpbak-wptest-20261112T100000Z.tar.gz"
    )
    assert capsys.readouterr().out.strip() == str(expected)
    assert expected.is_file()
    for made in (expected.parent, expected.parent.parent, backups / "wptest"):
        assert stat.S_IMODE(made.stat().st_mode) == 0o700, made


def test_unset_stays_flat(site: Path, backups: Path, clock: Clock) -> None:
    assert main(_backup_argv(site, backups)) == 0
    (written,) = backups.iterdir()
    assert written.name == "wpbak-wptest-20261112T100000Z.tar.gz"


BAD = ("{host}/{yyyy}", "../{site}", "/abs", "{site:>4}", "a//b")


@pytest.mark.parametrize("spelling", ["flag", "variable", "config"])
@pytest.mark.parametrize("template", BAD)
def test_a_bad_template_is_a_usage_error_from_every_spelling(
    site: Path,
    backups: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    spelling: str,
    template: str,
) -> None:
    monkeypatch.setenv("L3IO_WP_BACKUP_PASSPHRASE", SECRET)
    argv = _backup_argv(site, backups)
    if spelling == "flag":
        argv += ["--archive-subdir", template]
    elif spelling == "variable":
        monkeypatch.setenv("L3IO_WP_BACKUP_ARCHIVE_SUBDIR", template)
    else:
        config = tmp_path / "config.toml"
        config.write_text(f"archive_subdir = {template!r}\n", encoding="utf-8")
        argv = ["--config", str(config), *argv]
    with pytest.raises(SystemExit) as caught:
        main(argv)
    assert caught.value.code == ExitCode.USAGE
    err = capsys.readouterr().err
    assert "archive-subdir" in err or "ARCHIVE_SUBDIR" in err or "archive_subdir" in err
    assert SECRET not in err
    assert _tree(backups) == set()


# Each bad template's own reason, from every spelling: the flag's through
# argparse, the variable's and the key's through the layering.
REASONS = {
    "{host}/{yyyy}": "has an unknown token {host}",
    "../{site}": "has a '..' segment",
    "/abs": "is absolute",
    "{site:>4}": "has a format spec or conversion in a token",
    "a//b": "has an empty segment",
    "None": "is ambiguous",
}


@pytest.mark.parametrize("spelling", ["flag", "variable", "config"])
@pytest.mark.parametrize("template", REASONS)
def test_a_bad_template_gets_its_reason_from_every_spelling(
    site: Path,
    backups: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    spelling: str,
    template: str,
) -> None:
    monkeypatch.setenv("L3IO_WP_BACKUP_PASSPHRASE", SECRET)
    argv = _backup_argv(site, backups)
    where = "--archive-subdir"
    if spelling == "flag":
        argv += ["--archive-subdir", template]
    elif spelling == "variable":
        monkeypatch.setenv("L3IO_WP_BACKUP_ARCHIVE_SUBDIR", template)
        where = "L3IO_WP_BACKUP_ARCHIVE_SUBDIR"
    else:
        config = tmp_path / "config.toml"
        config.write_text(f"archive_subdir = {template!r}\n", encoding="utf-8")
        argv = ["--config", str(config), *argv]
        where = "config file key 'archive_subdir'"
    with pytest.raises(SystemExit) as caught:
        main(argv)
    assert caught.value.code == ExitCode.USAGE
    err = capsys.readouterr().err
    assert where in err
    assert REASONS[template] in err
    assert "a relative path under the archive directory" in err
    assert SECRET not in err
    assert _tree(backups) == set()


@pytest.mark.parametrize("spelling", ["flag", "variable", "config"])
def test_none_is_flat_from_every_spelling(
    site: Path,
    backups: Path,
    clock: Clock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spelling: str,
) -> None:
    argv = _backup_argv(site, backups)
    if spelling == "flag":
        argv += ["--archive-subdir", "none"]
    elif spelling == "variable":
        monkeypatch.setenv("L3IO_WP_BACKUP_ARCHIVE_SUBDIR", "none")
    else:
        config = tmp_path / "config.toml"
        config.write_text('archive_subdir = "none"\n', encoding="utf-8")
        argv = ["--config", str(config), *argv]
    assert main(argv) == 0
    (written,) = backups.iterdir()
    assert written.name == "wpbak-wptest-20261112T100000Z.tar.gz"


@pytest.mark.parametrize("over", ["variable", "config"])
def test_none_on_the_command_line_overrides_a_template_for_one_run(
    site: Path,
    backups: Path,
    clock: Clock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    over: str,
) -> None:
    argv = _backup_argv(site, backups, "--archive-subdir", "none")
    if over == "variable":
        monkeypatch.setenv("L3IO_WP_BACKUP_ARCHIVE_SUBDIR", TEMPLATE)
    else:
        config = tmp_path / "config.toml"
        config.write_text(f'archive_subdir = "{TEMPLATE}"\n', encoding="utf-8")
        argv = ["--config", str(config), *argv]
    assert main(argv) == 0
    (written,) = backups.iterdir()
    assert written.is_file()
    assert written.name == "wpbak-wptest-20261112T100000Z.tar.gz"


def test_none_in_the_environment_overrides_the_config_file(
    site: Path,
    backups: Path,
    clock: Clock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(f'archive_subdir = "{TEMPLATE}"\n', encoding="utf-8")
    monkeypatch.setenv("L3IO_WP_BACKUP_ARCHIVE_SUBDIR", "none")
    assert main(["--config", str(config), *_backup_argv(site, backups)]) == 0
    (written,) = backups.iterdir()
    assert written.is_file()


@pytest.mark.parametrize("ambiguous", ["None", "NONE", "nOnE"])
def test_a_case_variant_of_none_is_refused_not_made_a_folder(
    site: Path,
    backups: Path,
    capsys: pytest.CaptureFixture[str],
    ambiguous: str,
) -> None:
    with pytest.raises(SystemExit) as caught:
        main(_backup_argv(site, backups, "--archive-subdir", ambiguous))
    assert caught.value.code == ExitCode.USAGE
    assert "write 'none'" in capsys.readouterr().err
    assert _tree(backups) == set()


def test_a_site_name_that_would_climb_out_is_refused(
    site: Path, backups: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = _backup_argv(
        site, backups, "--archive-subdir", "{site}", "--site-name", ".."
    )
    assert main(argv) == ExitCode.USAGE
    assert "escape" in capsys.readouterr().err
    assert _tree(backups) == set()
    assert {p.name for p in backups.parent.iterdir()} >= {"backups", "www"}


def test_a_symlinked_subfolder_is_refused(
    site: Path, backups: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (backups / "wptest").symlink_to(elsewhere, target_is_directory=True)
    argv = _backup_argv(site, backups, "--archive-subdir", "{site}/{yyyy}")
    assert main(argv) == ExitCode.USAGE
    assert "symlink" in capsys.readouterr().err
    assert list(elsewhere.iterdir()) == []


def test_the_dry_run_shows_the_resolved_path_and_creates_nothing(
    site: Path, backups: Path, clock: Clock, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["--whatIf", *_backup_argv(site, backups, "--archive-subdir", TEMPLATE)]
    capsys.readouterr()
    assert main(argv) == 0
    out = capsys.readouterr().out
    expected = (
        backups / "wptest" / "2026" / "11" / "wpbak-wptest-20261112T100000Z.tar.gz"
    )
    assert str(expected) in out
    assert _tree(backups) == set()


def test_an_explicit_archive_overrides_the_subfolder(
    site: Path, backups: Path, tmp_path: Path
) -> None:
    named = tmp_path / "named.tar.gz"
    argv = _backup_argv(
        site, backups, "--archive-subdir", TEMPLATE, "--archive", str(named)
    )
    assert main(argv) == 0
    assert named.is_file()
    assert _tree(backups) == set()


def test_a_nested_earlier_archive_in_the_site_root_is_not_collected(
    site: Path, clock: Clock
) -> None:
    inside = site / "backups"
    inside.mkdir()
    argv = _backup_argv(site, inside, "--archive-subdir", TEMPLATE)
    assert main(argv) == 0
    clock.now = datetime(2026, 12, 1, tzinfo=UTC)
    assert main(argv) == 0
    first = inside / "wptest" / "2026" / "11" / "wpbak-wptest-20261112T100000Z.tar.gz"
    second = inside / "wptest" / "2026" / "12" / "wpbak-wptest-20261201T000000Z.tar.gz"
    with tarfile.open(second) as outer:
        member = outer.extractfile(PAYLOAD_MEMBER)
        assert member is not None
        with tarfile.open(fileobj=member, mode="r|*") as inner:
            names = [entry.name for entry in inner]
    assert any(name.endswith("index.php") for name in names)
    assert not any(name.endswith(first.name) for name in names)


def test_the_stale_partial_sweep_reaches_every_subfolder(
    site: Path, backups: Path, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    old = backups / "wptest" / "2026" / "10"
    old.mkdir(parents=True)
    stale = old / ".wpbak-wptest-20261031T030000Z.tar.gz.k1.partial"
    fresh = old / ".wpbak-wptest-20261031T040000Z.tar.gz.k2.partial"
    for each in (stale, fresh):
        each.write_bytes(b"plaintext")
    when = time.time() - STALE_AFTER_SECONDS - 60
    os.utime(stale, (when, when))
    assert main(_backup_argv(site, backups, "--archive-subdir", TEMPLATE)) == 0
    assert not stale.exists()
    assert fresh.exists()
    removed = [
        e for e in pipeline_events(caplog) if e["event"] == "stale_residue_removed"
    ]
    assert [e["path"] for e in removed] == [str(stale)]


# --------------------------------------------------------------------------
# Discovery: latest and prune over flat and nested, old and new names
# --------------------------------------------------------------------------


class Estate:
    """This site's Archives, flat and nested, and another site's newest one."""

    def __init__(self, tmp: Path, backups: Path, clock: Clock) -> None:
        self.backups = backups
        self.site = _site(tmp / "shop", "shopdb")
        other = _site(tmp / "other", "otherdb")
        self.ours: list[Path] = []
        # Oct 1: flat, renamed to the legacy name.
        clock.now = datetime(2026, 10, 1, tzinfo=UTC)
        flat = self._backup(self.site)
        legacy = flat.with_name("wpbackup2-shopdb-20261001000000.tar.gz")
        flat.rename(legacy)
        self.ours.append(legacy)
        # Oct 15: flat, today's name.
        clock.now = datetime(2026, 10, 15, tzinfo=UTC)
        self.ours.append(self._backup(self.site))
        # Nov 2 and Nov 10: nested by month; Dec 1: nested, the newest.
        for when in ((2026, 11, 2), (2026, 11, 10), (2026, 12, 1)):
            clock.now = datetime(*when, tzinfo=UTC)
            self.ours.append(self._backup(self.site, "--archive-subdir", TEMPLATE))
        # Another site's, newer than all of ours, in its own subfolder.
        clock.now = datetime(2026, 12, 2, tzinfo=UTC)
        self.foreign = self._backup(other, "--archive-subdir", TEMPLATE)

    def _backup(self, site: Path, *extra: str) -> Path:
        before = _tree(self.backups)
        assert main(_backup_argv(site, self.backups, *extra)) == 0
        (written,) = {p for p in _tree(self.backups) - before if p.is_file()}
        return written


@pytest.fixture
def estate(tmp_path: Path, workdir: Path, backups: Path, clock: Clock) -> Estate:
    del workdir
    return Estate(tmp_path, backups, clock)


def test_the_estate_is_flat_and_nested(estate: Estate) -> None:
    assert estate.ours[0].parent == estate.backups
    assert estate.ours[1].parent == estate.backups
    assert estate.ours[2].parent == estate.backups / "shopdb" / "2026" / "11"
    assert estate.ours[4].parent == estate.backups / "shopdb" / "2026" / "12"
    assert estate.foreign.parent == estate.backups / "otherdb" / "2026" / "12"


def test_latest_is_the_newest_across_flat_nested_old_and_new(
    estate: Estate, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["--log-format", "console", "--whatIf", "restore", "--wp-dir"]
    argv += [str(estate.site), "--mode", "files"]
    argv += ["--archive", "latest", "--archive-dir", str(estate.backups)]
    capsys.readouterr()
    assert main(argv) == 0
    out = capsys.readouterr().out
    assert f"archive latest: {estate.ours[4]} (" in out
    assert "the newest of 5 archives of this site" in out


def test_latest_finds_a_legacy_flat_archive_when_it_is_the_newest(
    estate: Estate, capsys: pytest.CaptureFixture[str]
) -> None:
    for path in estate.ours[1:]:
        path.unlink()
    argv = ["--log-format", "console", "--whatIf", "restore", "--wp-dir"]
    argv += [str(estate.site), "--mode", "files"]
    argv += ["--archive", "latest", "--archive-dir", str(estate.backups)]
    capsys.readouterr()
    assert main(argv) == 0
    assert f"archive latest: {estate.ours[0]} (" in capsys.readouterr().out


def _prune(estate: Estate, *extra: str) -> list[str]:
    return [
        "--log-format",
        "json",
        "prune",
        "--wp-dir",
        str(estate.site),
        "--archive-dir",
        str(estate.backups),
        *extra,
    ]


def test_prune_reaches_every_folder_and_removes_only_those_it_emptied(
    estate: Estate,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    (estate.backups / "unrelated-empty").mkdir()
    capsys.readouterr()
    # The newest of each month: Oct 15, Nov 10, Dec 1.
    assert main(_prune(estate, "--keep-monthly", "3")) == 0
    assert not estate.ours[0].exists()
    assert not estate.ours[2].exists()
    assert all(p.exists() for p in (estate.ours[1], estate.ours[3], estate.ours[4]))
    assert estate.foreign.exists()
    events = pipeline_events(caplog)
    assert {e["path"] for e in events if e["event"] == "archive_pruned"} == {
        str(estate.ours[0]),
        str(estate.ours[2]),
    }
    # November still holds Nov 10: nothing emptied, nothing removed.
    assert [e for e in events if e["event"] == "archive_dir_removed"] == []
    assert (estate.backups / "unrelated-empty").is_dir()
    # Keep only the newest: November's folder empties and goes; 2026 and
    # shopdb still hold December, and the Archive directory always stays.
    capsys.readouterr()
    assert main(_prune(estate, "--keep", "1")) == 0
    november = estate.backups / "shopdb" / "2026" / "11"
    assert not november.exists()
    assert (estate.backups / "shopdb" / "2026" / "12").is_dir()
    removed = [
        e for e in pipeline_events(caplog) if e["event"] == "archive_dir_removed"
    ]
    assert [e["path"] for e in removed] == [str(november)]
    out = capsys.readouterr().out.splitlines()
    assert f"removed empty folder {november}" in out
    assert estate.backups.is_dir()
    assert (estate.backups / "unrelated-empty").is_dir()


def test_a_prune_that_empties_a_whole_branch_removes_it_up_to_the_root(
    estate: Estate, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    # December goes by hand, and a newer flat Archive becomes the newest: a
    # prune keeping one then empties November, 2026 and shopdb in turn.
    estate.ours.pop().unlink()
    (estate.backups / "shopdb" / "2026" / "12").rmdir()
    clock.now = datetime(2026, 12, 5, tzinfo=UTC)
    newest = estate._backup(estate.site)  # noqa: SLF001
    assert newest.parent == estate.backups
    assert main(_prune(estate, "--keep", "1")) == 0
    removed = [
        e["path"]
        for e in pipeline_events(caplog)
        if e["event"] == "archive_dir_removed"
    ]
    shop = estate.backups / "shopdb"
    assert removed == [str(shop / "2026" / "11"), str(shop / "2026"), str(shop)]
    assert not shop.exists()
    assert estate.foreign.exists()
    assert newest.exists()
    assert estate.backups.is_dir()


def test_a_dry_run_prune_removes_no_folder_and_lists_those_it_would(
    estate: Estate,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    before = _tree(estate.backups)
    capsys.readouterr()
    assert main(["--whatIf", *_prune(estate, "--keep", "1")]) == 0
    assert _tree(estate.backups) == before
    november = estate.backups / "shopdb" / "2026" / "11"
    events = pipeline_events(caplog)
    removed = [e for e in events if e["event"] == "archive_dir_removed"]
    assert [(e["path"], e["performed"]) for e in removed] == [(str(november), False)]
    (done,) = [e for e in events if e["event"] == "prune_completed"]
    assert done["removed_directories"] == [str(november)]
    assert done["performed"] is False
    out = capsys.readouterr().out.splitlines()
    assert f"would remove empty folder {november}" in out
    assert not [line for line in out if line.startswith("removed empty folder")]


def test_a_dry_run_lists_exactly_what_the_real_prune_then_removes(
    estate: Estate, clock: Clock
) -> None:
    """A whole branch, a folder holding a stranger and a symlink: the Dry Run
    predicts the real prune's folders, in its order."""
    estate.ours.pop().unlink()
    (estate.backups / "shopdb" / "2026" / "12").rmdir()
    clock.now = datetime(2026, 12, 5, tzinfo=UTC)
    estate._backup(estate.site)  # noqa: SLF001
    (estate.backups / "shopdb" / "2026" / "10").mkdir()
    (estate.backups / "shopdb" / "2026" / "10" / "notes.txt").write_text("x\n")
    dry = api.prune(estate.site, keep=1, archive_dir=estate.backups, dry_run=True)
    assert dry.performed is False
    assert dry.removed_directories == (
        PurePosixPath(estate.backups / "shopdb" / "2026" / "11"),
    )
    real = api.prune(estate.site, keep=1, archive_dir=estate.backups)
    assert real.removed_directories == dry.removed_directories
    assert (estate.backups / "shopdb" / "2026" / "10" / "notes.txt").exists()


def test_a_dry_run_never_lists_a_symlinked_folder(estate: Estate) -> None:
    target = estate.backups.parent / "elsewhere"
    target.mkdir()
    link = estate.backups / "shopdb" / "2026" / "11" / "link"
    link.symlink_to(target, target_is_directory=True)
    dry = api.prune(estate.site, keep=1, archive_dir=estate.backups, dry_run=True)
    assert dry.removed_directories == ()


def test_the_library_reports_the_folders_it_removed(estate: Estate) -> None:
    result = api.prune(estate.site, keep=1, archive_dir=estate.backups)
    assert {p.path.name for p in result.pruned} == {p.name for p in estate.ours[:4]}
    assert [str(d) for d in result.removed_directories] == [
        str(estate.backups / "shopdb" / "2026" / "11")
    ]
