"""Phase 3 findings — a restore its user cannot write, and why no Safety Snapshot.

A restore whose Site Root the Run's effective user cannot write used to pass
``PREFLIGHT`` and ``VERIFY`` and fail at ``EXTRACT`` ("could not write
archive '/var/www/html'"), leaving an interrupted Run. Now the target survey
finds it and the Run is refused before any effect, a Dry Run too, naming the
path and the uid (``TargetNotWritableError``, exit 3), and the next Run needs
no ``--resolve-interrupted-run``.

The Run Journal records why there is no Safety Snapshot (``snapshot_skipped``):
an empty target needs none, ``--no-safety-snapshot`` opts out; the undo text
says which (``tests/unit/core/test_restore_journal.py``).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from l3io.wp.backup.adapters.journal import FileRunJournal
from l3io.wp.backup.cli import main
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.domain.journal import JournalHeader

_AS_ROOT = pytest.mark.skipif(
    os.geteuid() == 0, reason="root writes whatever the mode bits"
)


def _site(root: Path) -> Path:
    root.mkdir()
    (root / "wp-config.php").write_text(
        "<?php\ndefine('DB_NAME', 'wptest');\ndefine('DB_USER', 'wp');\n"
        "define('DB_PASSWORD', 'not-a-real-secret');\n"
        "define('DB_HOST', 'localhost');\n$table_prefix = 'wp_';\n",
        encoding="utf-8",
    )
    (root / "index.php").write_text("<?php // archived\n", encoding="utf-8")
    return root


@pytest.fixture
def archived(tmp_path: Path, workdir: Path) -> tuple[Path, Path]:
    """A files Archive of a site, and that site."""
    del workdir
    site = _site(tmp_path / "www")
    archive = tmp_path / "site.tar.gz"
    argv = ["backup", "--wp-dir", str(site), "--mode", "files", "--archive"]
    assert main([*argv, str(archive)]) == 0
    (site / "index.php").write_text("<?php // changed since\n", encoding="utf-8")
    return site, archive


def _restore(site: Path, archive: Path, *extra: str) -> list[str]:
    return [
        "--log-format",
        "json",
        *extra,
        "restore",
        "--wp-dir",
        str(site),
        "--archive",
        str(archive),
        "--mode",
        "files",
    ]


@_AS_ROOT
@pytest.mark.parametrize("dry_run", [False, True], ids=["run", "dry-run"])
def test_a_site_root_the_run_cannot_write_is_refused_before_any_effect(
    archived: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    dry_run: bool,
) -> None:
    site, archive = archived
    site.chmod(0o555)
    try:
        code = main(_restore(site, archive, *(["--whatIf"] if dry_run else [])))
    finally:
        site.chmod(0o755)
    err = capsys.readouterr().err
    assert code == ExitCode.REFUSED
    assert "TargetNotWritableError" in err
    assert repr(str(site)) in err
    assert f"uid {os.geteuid()}" in err
    assert "could not write archive" not in err
    assert (site / "index.php").read_text(
        encoding="utf-8"
    ) == "<?php // changed since\n"
    # Nothing was begun: the next Run needs no --resolve-interrupted-run.
    assert main(_restore(site, archive)) == 0
    assert (site / "index.php").read_text(encoding="utf-8") == "<?php // archived\n"


@_AS_ROOT
def test_a_file_the_run_cannot_replace_is_named(
    archived: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    site, archive = archived
    (site / "index.php").chmod(0o444)
    code = main(_restore(site, archive))
    err = capsys.readouterr().err
    assert code == ExitCode.REFUSED
    assert repr(str(site / "index.php")) in err


@pytest.fixture
def headers(monkeypatch: pytest.MonkeyPatch) -> list[JournalHeader]:
    begun: list[JournalHeader] = []
    real = FileRunJournal.begin_run

    def begin_run(self: FileRunJournal, header: JournalHeader) -> None:
        begun.append(header)
        real(self, header)

    monkeypatch.setattr(FileRunJournal, "begin_run", begin_run)
    return begun


def test_an_empty_target_records_that_no_snapshot_was_needed(
    archived: tuple[Path, Path], tmp_path: Path, headers: list[JournalHeader]
) -> None:
    _site_root, archive = archived
    empty = tmp_path / "empty"
    empty.mkdir()
    # Unencrypted, so the Archive's database is confirmed (owner decision 65).
    argv = [*_restore(empty, archive), "--archive-database", "wp@localhost/wptest"]
    assert main(argv) == 0
    (header,) = headers
    assert header.snapshot_path is None
    assert header.snapshot_skipped == "target_empty"


def test_opting_out_is_recorded_as_opting_out(
    archived: tuple[Path, Path], headers: list[JournalHeader]
) -> None:
    site, archive = archived
    assert main([*_restore(site, archive), "--no-safety-snapshot"]) == 0
    (header,) = headers
    assert header.snapshot_path is None
    assert header.snapshot_skipped == "opted_out"


def test_a_snapshot_taken_records_no_reason(
    archived: tuple[Path, Path], headers: list[JournalHeader]
) -> None:
    site, archive = archived
    assert main(_restore(site, archive)) == 0
    (header,) = headers
    assert header.snapshot_path is not None
    assert header.snapshot_skipped is None
