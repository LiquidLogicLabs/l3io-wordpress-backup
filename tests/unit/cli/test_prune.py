"""Owner decision 83 — ``wpbackup2 prune --keep N``, through the CLI and the library.

Real Archives written by ``backup`` (the clock pinned, so their ``created_at``
order is known) in one directory, beside what must survive every prune:
another site's Archive older than all of them, a Safety Snapshot of this site,
a file that is no Archive, a symlink to one of this site's Archives and a
staged ``.partial`` file. Only this site's oldest go; the newest ``keep``
stay; ``--whatIf`` deletes nothing; ``--keep`` is required, at least 1, and
has its three spellings; the Site Lock is taken; the library has the same
call.
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
    assert out[0].startswith(f"pruned {layout.ours[1]} (")
    assert out[-1].startswith("pruned 2 of this site's 4 archives in ")


def test_a_dry_run_lists_and_deletes_nothing(
    layout: Layout, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    before = _present(layout)
    capsys.readouterr()
    assert main([*layout.prune("--keep", "1"), "--whatIf"]) == 0
    assert _present(layout) == before
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 4
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


@pytest.mark.parametrize(
    "extra", [(), ("--keep", "0"), ("--keep", "-1"), ("--keep", "two")]
)
def test_keep_is_required_and_at_least_one(
    layout: Layout, capsys: pytest.CaptureFixture[str], extra: tuple[str, ...]
) -> None:
    before = _present(layout)
    with pytest.raises(SystemExit) as caught:
        main(layout.prune(*extra))
    assert caught.value.code == ExitCode.USAGE
    assert "--keep" in capsys.readouterr().err
    assert _present(layout) == before


def test_keep_zero_from_a_variable_is_refused(
    layout: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _present(layout)
    monkeypatch.setenv("L3IO_WP_BACKUP_KEEP", "0")
    assert main(layout.prune()) == ExitCode.USAGE
    assert _present(layout) == before


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
    assert json.dumps([str(p) for p in result.kept])


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
