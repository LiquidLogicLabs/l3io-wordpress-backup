"""AD-18.1, ADR-0021 — ``adapters/journal.py`` on a real filesystem (E003-S02-005)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path, PurePosixPath

import pytest

from l3io.wp.backup.adapters import journal as journal_module
from l3io.wp.backup.adapters.journal import (
    CURRENT,
    CorruptJournalError,
    FileRunJournal,
    JournalWriteError,
    journal_directory,
    refuse_incomplete,
)
from l3io.wp.backup.core.restore import NO_RELOCATION
from l3io.wp.backup.core.restore_journal import (
    STEP_CUTOVER,
    STEP_CUTOVER_STARTED,
    IncompleteRunError,
)
from l3io.wp.backup.domain.errors import RefusedError
from l3io.wp.backup.domain.journal import JournalEntry
from l3io.wp.backup.domain.restore import RestoreMode, RestorePhase
from tests.load_fakes import load_header

SITE = PurePosixPath("/srv/www")
#: One recorded write sequence: (method, args) in the order a Run writes them.
SEQUENCE: list[tuple[str, tuple[object, ...]]] = [
    ("phase_started", (RestorePhase.CLEAN, "/srv/www")),
    ("phase_completed", (RestorePhase.CLEAN,)),
    ("phase_started", (RestorePhase.LOAD_DUMP, "wp")),
    ("step_completed", (RestorePhase.LOAD_DUMP, STEP_CUTOVER_STARTED + "DB_NAME")),
    ("step_completed", (RestorePhase.LOAD_DUMP, STEP_CUTOVER + "DB_NAME")),
    ("phase_completed", (RestorePhase.LOAD_DUMP,)),
]
EVENT = {"phase_started", "step_completed", "phase_completed"}


def _header(run_id: str = "run-1") -> object:
    return load_header(
        run_id=run_id,
        site_root=SITE,
        live="wp",
        relocation=NO_RELOCATION,
        mode=RestoreMode.ALL_CLEAN,
        archive="/backups/a.tar.gz",
        config_path="/srv/www/wp-config.php",
    )


def _begun(tmp_path: Path, run_id: str = "run-1") -> FileRunJournal:
    journal = FileRunJournal(tmp_path / "journal")
    journal.begin_run(_header(run_id))  # type: ignore[arg-type]
    return journal


def _expected(prefix: list[tuple[str, tuple[object, ...]]]) -> list[tuple[object, ...]]:
    out: list[tuple[object, ...]] = []
    for method, args in prefix:
        step = args[1] if method == "step_completed" else None
        target = args[1] if method == "phase_started" else None
        out.append((method, args[0], target, step))
    return out


def _seen(entries: tuple[JournalEntry, ...]) -> list[tuple[object, ...]]:
    return [(e.event, e.phase, e.target, e.step) for e in entries]


def test_incomplete_returns_each_prefix_of_the_write_sequence(tmp_path: Path) -> None:
    journal = _begun(tmp_path)
    reader = FileRunJournal(tmp_path / "journal")
    record = reader.incomplete(SITE)
    assert record is not None
    assert record.header == _header()
    assert record.entries == ()
    for n, (method, args) in enumerate(SEQUENCE, start=1):
        assert method in EVENT
        getattr(journal, method)(*args)
        record = reader.incomplete(SITE)
        assert record is not None
        assert _seen(record.entries) == _expected(SEQUENCE[:n])


def test_no_journal_is_none(tmp_path: Path) -> None:
    assert FileRunJournal(tmp_path / "nothing").incomplete(SITE) is None
    refuse_incomplete(FileRunJournal(tmp_path / "nothing"), SITE)


def test_a_crash_between_temp_write_and_replace_keeps_the_previous_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = _begun(tmp_path)
    journal.phase_started(RestorePhase.CLEAN, "/srv/www")
    before = (journal.journal_path).read_bytes()

    def crash(self: Path, target: Path) -> Path:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "replace", crash)
    with pytest.raises(JournalWriteError):
        journal.phase_completed(RestorePhase.CLEAN)
    monkeypatch.undo()
    assert journal.journal_path.read_bytes() == before
    record = FileRunJournal(journal.directory).incomplete(SITE)
    assert record is not None
    assert [e.event for e in record.entries] == ["phase_started"]


def test_the_write_sequence_is_temp_fsync_replace_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    real_fsync, real_replace, real_open = os.fsync, Path.replace, os.open
    real_link = os.link

    def link(source: object, target: object) -> None:
        calls.append(f"link:{Path(str(source)).name}->{Path(str(target)).name}")
        real_link(source, target)  # type: ignore[arg-type]

    def fsync(fd: int) -> None:
        kind = "dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
        calls.append(f"fsync:{kind}")
        real_fsync(fd)

    def replace(self: Path, target: Path) -> Path:
        calls.append(f"replace:{self.name}->{Path(target).name}")
        return real_replace(self, target)

    # Keywords pass through: the patch outlives the test body (monkeypatch is
    # undone after tmp_path's teardown, whose rmtree calls os.open(dir_fd=...)).
    def opened(path: object, flags: int, mode: int = 0o777, **kw: object) -> int:
        if flags & os.O_CREAT:
            assert flags & os.O_TRUNC
            calls.append(f"open:{Path(str(path)).name}:{mode:o}")
        return real_open(path, flags, mode, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(Path, "replace", replace)
    monkeypatch.setattr(journal_module.os, "open", opened)
    monkeypatch.setattr(journal_module.os, "link", link)
    journal = _begun(tmp_path)
    # BL-E003-077: the first record is published by link(2), never replacing.
    assert calls == [
        "open:current.json.tmp:600",
        "fsync:file",
        f"link:current.json.tmp->{CURRENT}",
        "fsync:dir",
    ]
    calls.clear()
    journal.phase_started(RestorePhase.SNAPSHOT, "files")
    assert calls == [
        "open:current.json.tmp:600",
        "fsync:file",
        f"replace:current.json.tmp->{CURRENT}",
        "fsync:dir",
    ]
    assert not (tmp_path / "journal" / "current.json.tmp").exists()


def test_begin_never_replaces_a_record_that_appears_after_any_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BL-E003-077: a check-then-write would replace another Run's record."""
    other = _begun(tmp_path, run_id="run-other")
    before = other.journal_path.read_bytes()
    real_exists = Path.exists

    def unseen(self: Path, *args: object, **kwargs: object) -> bool:
        # The record arrives between any existence check and the write.
        return False if self.name == CURRENT else real_exists(self)

    monkeypatch.setattr(Path, "exists", unseen)
    second = FileRunJournal(tmp_path / "journal")
    with pytest.raises(JournalWriteError, match="already present"):
        second.begin_run(_header("run-2"))  # type: ignore[arg-type]
    assert other.journal_path.read_bytes() == before
    assert not (tmp_path / "journal" / "current.json.tmp").exists()


def test_begin_creates_every_missing_directory_owner_only(tmp_path: Path) -> None:
    """BL-E003-077: the state directory's ancestors are ``0700``, not the umask's."""
    existing = stat.S_IMODE(tmp_path.stat().st_mode)
    previous = os.umask(0o022)
    try:
        journal = FileRunJournal(tmp_path / "state" / "snapshots" / "digest")
        journal.begin_run(_header())  # type: ignore[arg-type]
    finally:
        os.umask(previous)
    for directory in (
        tmp_path / "state",
        tmp_path / "state" / "snapshots",
        tmp_path / "state" / "snapshots" / "digest",
    ):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700, directory
    assert stat.S_IMODE(tmp_path.stat().st_mode) == existing


def test_a_corrupt_journal_refuses_and_is_never_absent(tmp_path: Path) -> None:
    directory = tmp_path / "journal"
    directory.mkdir()
    (directory / CURRENT).write_text("{not json", encoding="utf-8")
    journal = FileRunJournal(directory)
    with pytest.raises(CorruptJournalError) as caught:
        journal.incomplete(SITE)
    assert caught.value.journal_path == str(directory / CURRENT)
    assert isinstance(caught.value, RefusedError)
    assert "could not be determined" in str(caught.value)
    assert "move that file aside" in str(caught.value)
    (directory / CURRENT).write_text(json.dumps({"header": {}, "entries": []}))
    with pytest.raises(CorruptJournalError):
        refuse_incomplete(journal, SITE)


@pytest.mark.parametrize(
    ("complete", "folder"), [(True, "completed"), (False, "failed")]
)
def test_finish_run_moves_the_record(
    tmp_path: Path, complete: bool, folder: str
) -> None:
    journal = _begun(tmp_path)
    journal.phase_started(RestorePhase.CLEAN, "/srv/www")
    if complete:
        journal.phase_completed(RestorePhase.CLEAN)
    journal.finish_run()
    assert not journal.journal_path.exists()
    moved = journal.directory / folder / "run-1.json"
    assert json.loads(moved.read_text())["header"]["run_id"] == "run-1"
    assert journal.incomplete(SITE) is None


def test_resolve_moves_the_named_run_only(tmp_path: Path) -> None:
    journal = _begun(tmp_path)
    journal.phase_started(RestorePhase.CLEAN, "/srv/www")
    later = FileRunJournal(journal.directory)
    with pytest.raises(IncompleteRunError) as caught:
        refuse_incomplete(later, SITE)
    assert caught.value.run_id == "run-1"
    assert caught.value.interrupted_phase is RestorePhase.CLEAN
    with pytest.raises(RefusedError):
        later.resolve("run-2", "operator")
    assert later.journal_path.exists()
    later.resolve("run-1", "operator")
    assert not later.journal_path.exists()
    body = json.loads((journal.directory / "resolved" / "run-1.json").read_text())
    assert body["resolved_by"] == "operator"
    assert body["resolved_at"]
    refuse_incomplete(FileRunJournal(journal.directory), SITE)


def test_begin_run_never_replaces_an_unresolved_record(tmp_path: Path) -> None:
    journal = _begun(tmp_path)
    before = journal.journal_path.read_bytes()
    with pytest.raises(JournalWriteError):
        FileRunJournal(journal.directory).begin_run(_header("run-2"))  # type: ignore[arg-type]
    assert journal.journal_path.read_bytes() == before


def test_a_failed_write_raises_before_the_phase_and_keeps_the_record(
    tmp_path: Path,
) -> None:
    journal = _begun(tmp_path)
    before = journal.journal_path.read_bytes()
    (journal.directory / "current.json.tmp").mkdir()  # the temp cannot be opened
    with pytest.raises(JournalWriteError):
        journal.phase_started(RestorePhase.CLEAN, "/srv/www")
    assert journal.journal_path.read_bytes() == before


def test_modes_are_0600_and_0700(tmp_path: Path) -> None:
    journal = _begun(tmp_path)
    assert stat.S_IMODE(journal.directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(journal.journal_path.stat().st_mode) == 0o600
    journal.finish_run()
    for folder in ("completed",):
        assert stat.S_IMODE((journal.directory / folder).stat().st_mode) == 0o700
        moved = journal.directory / folder / "run-1.json"
        assert stat.S_IMODE(moved.stat().st_mode) == 0o600


def test_the_journal_directory_is_per_site_and_outside_the_site_root(
    tmp_path: Path,
) -> None:
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    (one / "alias").symlink_to(one)
    assert journal_directory(one) == journal_directory(one / "alias")
    assert journal_directory(one) != journal_directory(two)
    assert not journal_directory(one).is_relative_to(one)
    assert journal_directory(one).parent.name == "snapshots"
