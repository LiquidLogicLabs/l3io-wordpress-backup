"""Owner decisions 83, 84 — ``wpbackup2 prune``, through the CLI and the library.

Real Archives written by ``backup`` (the clock pinned, so their ``created_at``
order is known) in one directory, beside what must survive every prune:
another site's Archive older than all of them, a Safety Snapshot of this site,
a file that is no Archive, a symlink to one of this site's Archives and a
staged ``.partial`` file. Only this site's oldest go; the newest ``keep``
stay; ``--whatIf`` deletes nothing; at least one rule (``--keep`` or a tier)
is required, each at least 1, and each has its three spellings; the tiers
keep by UTC day and ISO week; the Site Lock is taken; the library has the
same call. Which Archive each tier keeps over a long history is
``core/test_retention.py``'s.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from l3io.wp.backup import api
from l3io.wp.backup.adapters.archive_dir import delete_archive, list_archives
from l3io.wp.backup.adapters.clock import SystemClock
from l3io.wp.backup.cli import main
from l3io.wp.backup.domain.exit_codes import ExitCode
from tests.conftest import pipeline_events
from tests.site_lock_holder import held_elsewhere


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


class Layout:
    """This site's four Archives (oldest first) and everything that must stay."""

    def __init__(self, tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        now = [datetime(2026, 10, 1, tzinfo=UTC)]
        monkeypatch.setattr(SystemClock, "now", lambda _self: now[0])
        self.backups = tmp / "backups"
        self.backups.mkdir()
        other = _site(tmp / "other", "otherdb")
        self.foreign = self._backup(other)  # the oldest file in the directory
        self.site = _site(tmp / "www", "wptest")
        self.ours: list[Path] = []
        for day in (2, 3, 4, 5):
            now[0] = datetime(2026, 10, day, tzinfo=UTC)
            self.ours.append(self._backup(self.site))
        # The oldest of ours under the legacy name: still this site's.
        legacy = self.ours[0].with_name("wpbackup2-wptest-20261002000000.tar.gz")
        self.ours[0].rename(legacy)
        self.ours[0] = legacy
        # A Safety Snapshot of this site, older than all of ours, put here.
        now[0] = datetime(2026, 10, 1, 12, tzinfo=UTC)
        argv = ["restore", "--wp-dir", str(self.site), "--mode", "files"]
        argv += ["--archive", str(self.ours[1]), "--snapshot-dir", str(self.backups)]
        assert main(argv) == 0
        (self.snapshot,) = set(self.backups.glob("*.tar.gz")) - {
            self.foreign,
            *self.ours,
        }
        self.notes = self.backups / "notes.tar.gz"
        self.notes.write_text("not an archive\n", encoding="utf-8")
        self.link = self.backups / "link.tar.gz"
        self.link.symlink_to(self.ours[0])
        self.partial = self.backups / ".wpbak-wptest-x.tar.gz.abc.partial"
        self.partial.write_bytes(b"staged")

    def _backup(self, site: Path) -> Path:
        before = set(self.backups.iterdir())
        argv = ["backup", "--wp-dir", str(site), "--mode", "files"]
        assert main([*argv, "--archive-dir", str(self.backups)]) == 0
        (written,) = set(self.backups.iterdir()) - before
        return written

    def survivors(self) -> set[Path]:
        return {self.foreign, self.snapshot, self.notes, self.link, self.partial}

    def prune(self, *extra: str) -> list[str]:
        return [
            "--log-format",
            "json",
            "prune",
            "--wp-dir",
            str(self.site),
            "--archive-dir",
            str(self.backups),
            *extra,
        ]


@pytest.fixture
def layout(tmp_path: Path, workdir: Path, monkeypatch: pytest.MonkeyPatch) -> Layout:
    del workdir
    return Layout(tmp_path, monkeypatch)


def _present(layout: Layout) -> set[Path]:
    return {p for p in layout.backups.iterdir() if p.is_symlink() or p.exists()}


def test_only_this_sites_oldest_archives_are_pruned(
    layout: Layout,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    capsys.readouterr()
    assert main(layout.prune("--keep", "2")) == 0
    assert _present(layout) == layout.survivors() | set(layout.ours[2:])
    events = pipeline_events(caplog)
    pruned = [e for e in events if e["event"] == "archive_pruned"]
    assert [e["path"] for e in pruned] == [str(layout.ours[1]), str(layout.ours[0])]
    assert all(e["performed"] is True and e["bytes"] > 0 for e in pruned)
    assert pruned[0]["created_at"].startswith("2026-10-03")
    skipped = {
        e["path"]: e["reason"] for e in events if e["event"] == "archive_prune_skipped"
    }
    assert skipped == {str(layout.link): "a symlink"}
    (done,) = [e for e in events if e["event"] == "prune_completed"]
    assert (done["kept"], done["pruned"], done["matched"]) == (2, 2, 4)
    out = capsys.readouterr().out.splitlines()
    assert out[:2] == [
        f"kept {layout.ours[3]} (created 2026-10-05T00:00:00Z): newest",
        f"kept {layout.ours[2]} (created 2026-10-04T00:00:00Z): newest",
    ]
    assert out[2].startswith(f"pruned {layout.ours[1]} (")
    assert out[2].endswith("): newest: not among the newest 2")
    assert out[-1].startswith("pruned 2 of this site's 4 archives in ")
    assert out[-1].endswith("; kept 2 (--keep 2)")


def test_a_dry_run_lists_and_deletes_nothing(
    layout: Layout, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    before = _present(layout)
    capsys.readouterr()
    assert main([*layout.prune("--keep", "1"), "--whatIf"]) == 0
    assert _present(layout) == before
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 5
    assert sum(line.startswith("would prune /") for line in out) == 3
    assert out[-1].startswith("would prune 3 of this site's 4 archives")
    pruned = [e for e in pipeline_events(caplog) if e["event"] == "archive_pruned"]
    assert len(pruned) == 3
    assert all(e["performed"] is False for e in pruned)


@pytest.mark.parametrize("spelling", ["variable", "config"])
def test_keep_from_a_variable_or_the_config_file(
    layout: Layout, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spelling: str
) -> None:
    argv = layout.prune()
    if spelling == "variable":
        monkeypatch.setenv("L3IO_WP_BACKUP_KEEP", "3")
    else:
        config = tmp_path / "config.toml"
        config.write_text("keep = 3\n", encoding="utf-8")
        argv = ["--config", str(config), *argv]
    assert main(argv) == 0
    assert _present(layout) == layout.survivors() | set(layout.ours[1:])


TIERS = ("keep_daily", "keep_weekly", "keep_monthly", "keep_yearly")


def _flag(setting: str) -> str:
    return "--" + setting.replace("_", "-")


@pytest.mark.parametrize(
    "extra",
    [
        ("--keep", "0"),
        ("--keep", "-1"),
        ("--keep", "two"),
        *[(_flag(t), value) for t in TIERS for value in ("0", "-2", "x", "1.5")],
    ],
)
def test_each_rule_is_at_least_one(
    layout: Layout, capsys: pytest.CaptureFixture[str], extra: tuple[str, ...]
) -> None:
    before = _present(layout)
    with pytest.raises(SystemExit) as caught:
        main(layout.prune(*extra))
    assert caught.value.code == ExitCode.USAGE
    assert extra[0] in capsys.readouterr().err
    assert _present(layout) == before


def test_at_least_one_rule_is_required(
    layout: Layout, capsys: pytest.CaptureFixture[str]
) -> None:
    before = _present(layout)
    with pytest.raises(SystemExit) as caught:
        main(layout.prune())
    assert caught.value.code == ExitCode.USAGE
    err = capsys.readouterr().err
    assert "at least one retention rule" in err
    assert all(_flag(name) in err for name in ("keep", *TIERS))
    assert _present(layout) == before


@pytest.mark.parametrize("spelling", ["variable", "config"])
@pytest.mark.parametrize("setting", ["keep", *TIERS])
def test_a_rule_of_zero_from_a_variable_or_a_key_is_refused(
    layout: Layout,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    setting: str,
    spelling: str,
) -> None:
    """An ``int`` from the environment or TOML skips the parser's ``type``."""
    before = _present(layout)
    argv = layout.prune()
    if spelling == "variable":
        monkeypatch.setenv(f"L3IO_WP_BACKUP_{setting.upper()}", "0")
    else:
        config = tmp_path / "config.toml"
        config.write_text(f"{setting} = 0\n", encoding="utf-8")
        argv = ["--config", str(config), *argv]
    with pytest.raises(SystemExit) as caught:
        main(argv)
    assert caught.value.code == ExitCode.USAGE
    assert f"{setting} must be a whole number of at least 1" in capsys.readouterr().err
    assert _present(layout) == before


#: Oct 2 (Fri), 3 (Sat), 4 (Sun) are ISO 2026-W40; Oct 5 (Mon) is W41.
#: What each tier keeps of ``layout.ours``, by index.
TIER_KEEPS = {
    "keep_daily": (3, {1, 2, 3}),
    "keep_weekly": (2, {2, 3}),
    "keep_monthly": (1, {3}),
    "keep_yearly": (1, {3}),
}


@pytest.mark.parametrize("spelling", ["flag", "variable", "config"])
@pytest.mark.parametrize("setting", TIERS)
def test_each_tier_from_a_flag_a_variable_or_the_config_file(
    layout: Layout,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    setting: str,
    spelling: str,
) -> None:
    count, kept = TIER_KEEPS[setting]
    argv = layout.prune()
    if spelling == "flag":
        argv = layout.prune(_flag(setting), str(count))
    elif spelling == "variable":
        monkeypatch.setenv(f"L3IO_WP_BACKUP_{setting.upper()}", str(count))
    else:
        config = tmp_path / "config.toml"
        config.write_text(f"{setting} = {count}\n", encoding="utf-8")
        argv = ["--config", str(config), *argv]
    assert main(argv) == 0
    assert _present(layout) == layout.survivors() | {layout.ours[i] for i in kept}


def test_tiers_and_keep_combine_and_log_their_reasons(
    layout: Layout,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    capsys.readouterr()
    argv = layout.prune("--keep", "1", "--keep-weekly", "2", "--keep-daily", "1")
    assert main(["--log-level", "debug", *argv]) == 0
    assert _present(layout) == layout.survivors() | set(layout.ours[2:])
    events = pipeline_events(caplog)
    kept = {e["path"]: e["reasons"] for e in events if e["event"] == "archive_kept"}
    assert kept == {
        str(layout.ours[3]): ["newest", "daily", "weekly"],
        str(layout.ours[2]): ["weekly"],
    }
    pruned = {e["path"]: e["reasons"] for e in events if e["event"] == "archive_pruned"}
    assert pruned[str(layout.ours[1])] == [
        "newest: not among the newest 1",
        "daily: older than the newest 1 days that have an archive",
        "weekly: not the newest of its week",
    ]
    (done,) = [e for e in events if e["event"] == "prune_completed"]
    assert done["rules"] == {"keep": 1, "keep_daily": 1, "keep_weekly": 2}
    assert [a["reasons"] for a in done["kept_archives"]] == [
        ["newest", "daily", "weekly"],
        ["weekly"],
    ]
    assert [a["path"] for a in done["pruned_archives"]] == [
        str(layout.ours[1]),
        str(layout.ours[0]),
    ]
    out = capsys.readouterr().out.splitlines()
    assert out[0].endswith(": newest, daily, weekly")
    assert out[-1].endswith("; kept 2 (--keep 1 --keep-daily 1 --keep-weekly 2)")


def test_prune_waits_for_no_one_it_is_refused_while_the_site_is_locked(
    layout: Layout,
) -> None:
    before = _present(layout)
    with held_elsewhere(layout.site):
        assert main(layout.prune("--keep", "1")) == ExitCode.LOCKED
    assert _present(layout) == before


def test_the_library_prunes_the_same_way(layout: Layout) -> None:
    result = api.prune(layout.site, keep=3, archive_dir=layout.backups, dry_run=True)
    assert [str(p.path) for p in result.pruned] == [str(layout.ours[0])]
    assert result.performed is False
    result = api.prune(layout.site, keep=3, archive_dir=layout.backups)
    assert result.performed is True
    assert _present(layout) == layout.survivors() | set(layout.ours[1:])
    assert json.dumps([str(p.path) for p in result.kept])
    assert result.policy == api.RetentionPolicy(keep=3)


def test_the_library_takes_the_tiers_and_needs_a_rule(layout: Layout) -> None:
    result = api.prune(layout.site, keep_weekly=2, archive_dir=layout.backups)
    assert [p.path.name for p in result.kept] == [
        layout.ours[3].name,
        layout.ours[2].name,
    ]
    assert result.kept[0].reasons == ("weekly",)
    assert result.pruned[0].reasons == ("weekly: not the newest of its week",)
    before = _present(layout)
    with pytest.raises(api.UsageError):
        api.prune(layout.site, archive_dir=layout.backups)
    with pytest.raises(api.UsageError):
        api.prune(layout.site, keep_daily=0, archive_dir=layout.backups)
    assert _present(layout) == before


def test_a_file_swapped_after_it_was_read_is_left_alone(tmp_path: Path) -> None:
    archive = tmp_path / "a.tar.gz"
    archive.write_bytes(b"x")
    (listed,), _ = list_archives(tmp_path)
    swapped = tmp_path / "swap"
    swapped.write_bytes(b"another file under the same name")
    with archive.open("rb"):  # the read inode stays allocated: no reuse
        swapped.replace(archive)
        assert delete_archive(listed) == "changed since it was read"
    assert archive.read_bytes() == b"another file under the same name"
    archive.unlink()
    assert delete_archive(listed) is not None  # gone: reported, not raised
