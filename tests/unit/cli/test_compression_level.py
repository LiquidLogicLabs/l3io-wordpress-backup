"""Owner decision 88 — ``compression_level`` end to end: CLI, settings, library.

``compression_level`` (``--compression-level``,
``L3IO_WP_BACKUP_COMPRESSION_LEVEL``, the ``compression_level`` key; the
library's ``compression_level=``) is the inner payload's gzip level, 1 to 9,
6 unless set. It applies to a backup and to a restore's Safety Snapshot. Every
spelling refuses a value outside 1-9 as a usage error before any Run. The
level is planned: ``backup_started`` and the ``WRITE_ARCHIVE`` planned action
carry it and the Dry Run's ``write Archive`` line names it.

The level written is read from the Archive's bytes (``tests/payload_level.py``).
"""

from __future__ import annotations

import inspect
import random
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from l3io.wp.backup import api
from l3io.wp.backup.api import operations
from l3io.wp.backup.cli import main
from l3io.wp.backup.cli.parser import build_parser
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.compression import DEFAULT_COMPRESSION_LEVEL
from l3io.wp.backup.domain.errors import UsageError
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.domain.restore import (
    ConfigPolicy,
    Override,
    OverrideSet,
    RestoreMode,
    SnapshotPolicy,
)
from tests.conftest import pipeline_events
from tests.payload_level import matching_levels, payload_stream

VARIABLE = "L3IO_WP_BACKUP_COMPRESSION_LEVEL"
SPELLINGS = ("flag", "variable", "config")
#: Levels 1, 6 and 9 deflate the site below differently (asserted below).
SEPARATED = frozenset({1, 6, 9})


@pytest.fixture
def text_site(site: Path) -> Path:
    """The CLI ``site`` plus ~256 KiB of varied text."""
    words = random.Random(88).choices(  # noqa: S311 - test data, not security
        ["post", "meta", "wp", "content", "user", "the", "and", "option", "id"],
        k=48_000,
    )
    (site / "post.html").write_text(
        " ".join(f"{w}{i % 97}" for i, w in enumerate(words)), encoding="utf-8"
    )
    return site


def _level(archive: Path) -> int:
    """The one level of 1, 6 and 9 that wrote ``archive``'s payload."""
    (level,) = matching_levels(payload_stream(archive)) & SEPARATED
    return level


def _spelled(
    argv: list[str],
    spelling: str,
    value: str,
    *,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    """``argv`` with the level set by ``spelling``."""
    if spelling == "flag":
        return [*argv, "--compression-level", value]
    if spelling == "variable":
        monkeypatch.setenv(VARIABLE, value)
        return argv
    config = tmp_path / "config.toml"
    config.write_text(f"compression_level = {value}\n", encoding="utf-8")
    return ["--config", str(config), *argv]


def _backup(site: Path, archive: Path) -> list[str]:
    return [
        "backup",
        "--wp-dir",
        str(site),
        "--mode",
        "files",
        "--archive",
        str(archive),
    ]


# --------------------------------------------------------------------------
# Backup
# --------------------------------------------------------------------------


def test_unset_the_payload_is_written_at_level_6(
    text_site: Path, tmp_path: Path
) -> None:
    archive = tmp_path / "default.tar.gz"
    assert main(_backup(text_site, archive)) == 0
    assert _level(archive) == 6


@pytest.mark.parametrize("spelling", SPELLINGS)
@pytest.mark.parametrize("level", [1, 9])
def test_each_spelling_sets_the_payload_level(
    text_site: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spelling: str,
    level: int,
) -> None:
    archive = tmp_path / "set.tar.gz"
    argv = _spelled(
        _backup(text_site, archive),
        spelling,
        str(level),
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
    assert main(argv) == 0
    assert _level(archive) == level


#: Each as a flag or variable is text; in TOML, the literal written as is.
BAD = ("0", "10", "-1", "6.5", "abc", "true", '"six"')


@pytest.mark.parametrize("spelling", SPELLINGS)
@pytest.mark.parametrize("value", BAD)
@pytest.mark.parametrize("command", ["backup", "restore"])
def test_a_level_outside_1_to_9_is_a_usage_error_from_every_spelling(
    site: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    spelling: str,
    value: str,
    command: str,
) -> None:
    archive = tmp_path / "never.tar.gz"
    argv = (
        _backup(site, archive)
        if command == "backup"
        else ["restore", "--wp-dir", str(site), "--archive", str(archive)]
    )
    if spelling != "config":
        value = value.strip('"')
    argv = _spelled(argv, spelling, value, tmp_path=tmp_path, monkeypatch=monkeypatch)
    with pytest.raises(SystemExit) as caught:
        main(argv)
    assert caught.value.code == ExitCode.USAGE
    err = capsys.readouterr().err
    assert any(
        name in err for name in ("--compression-level", VARIABLE, "compression_level")
    ), err
    # Refused as a bad level, not as an option or key nobody knows.
    assert "unrecognized arguments" not in err
    assert "unknown setting" not in err
    assert not archive.exists()


def test_the_level_is_planned_logged_and_shown_by_the_dry_run(
    site: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    archive = tmp_path / "planned.tar.gz"
    argv = ["--whatIf", *_backup(site, archive), "--compression-level", "3"]
    capsys.readouterr()
    assert main(argv) == 0
    out = capsys.readouterr().out
    assert "write Archive (payload gzip level 3)" in out
    events = pipeline_events(caplog)
    (started,) = [e for e in events if e["event"] == "backup_started"]
    assert started["compression_level"] == 3
    (write,) = [
        e
        for e in events
        if e["event"] == "planned_action" and e["step"] == "WRITE_ARCHIVE"
    ]
    assert write["compression_level"] == 3
    assert not archive.exists()


def test_the_parser_default_is_the_domain_default() -> None:
    """Pinned: the flag's default is 6 on both commands, from one constant."""
    assert DEFAULT_COMPRESSION_LEVEL == 6
    parser = build_parser()
    for command in ("backup", "restore"):
        (action,) = [
            a
            for a in parser.commands[command]._actions  # noqa: SLF001
            if a.dest == "compression_level"
        ]
        assert action.default == 6
        assert action.option_strings == ["--compression-level"]


# --------------------------------------------------------------------------
# Restore: the Safety Snapshot
# --------------------------------------------------------------------------


@pytest.fixture
def archived(text_site: Path, tmp_path: Path) -> Path:
    """A files Archive of ``text_site``, which then changes."""
    archive = tmp_path / "site.tar.gz"
    assert main(_backup(text_site, archive)) == 0
    (text_site / "index.php").write_text("<?php // changed since\n", encoding="utf-8")
    return archive


def _restore(site: Path, archive: Path, snapshots: Path) -> list[str]:
    return [
        "restore",
        "--wp-dir",
        str(site),
        "--archive",
        str(archive),
        "--mode",
        "files",
        "--snapshot-dir",
        str(snapshots),
    ]


def _snapshot(snapshots: Path) -> Path:
    (taken,) = snapshots.glob("*.tar.gz")
    return taken


def test_unset_the_safety_snapshot_is_written_at_level_6(
    text_site: Path, archived: Path, tmp_path: Path
) -> None:
    snapshots = tmp_path / "snapshots"
    assert main(_restore(text_site, archived, snapshots)) == 0
    assert _level(_snapshot(snapshots)) == 6


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_each_spelling_sets_the_safety_snapshot_level(
    text_site: Path,
    archived: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spelling: str,
) -> None:
    snapshots = tmp_path / "snapshots"
    argv = _spelled(
        _restore(text_site, archived, snapshots),
        spelling,
        "1",
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
    assert main(argv) == 0
    assert _level(_snapshot(snapshots)) == 1


# --------------------------------------------------------------------------
# The library
# --------------------------------------------------------------------------


def test_the_library_defaults_are_6() -> None:
    for function in (api.backup, api.restore):
        parameter = inspect.signature(function).parameters["compression_level"]
        assert parameter.default == 6, function.__name__


def test_the_library_backup_writes_at_the_level_given(
    text_site: Path, tmp_path: Path
) -> None:
    archive = tmp_path / "library.tar.gz"
    api.backup(text_site, scope=BackupScope.FILES, target=archive, compression_level=1)
    assert _level(archive) == 1


def test_the_library_restore_hands_the_level_to_the_snapshot(
    text_site: Path, archived: Path, tmp_path: Path
) -> None:
    snapshots = tmp_path / "snapshots"
    api.restore(
        text_site,
        archive=archived,
        mode=RestoreMode.FILES,
        overrides=OverrideSet(waived=frozenset(Override)),
        snapshot=SnapshotPolicy.TAKE,
        config_policy=ConfigPolicy.KEEP,
        snapshot_dir=snapshots,
        compression_level=9,
    )
    assert _level(_snapshot(snapshots)) == 9


@pytest.mark.parametrize("level", [0, 10, True, 6.0, "6"])
@pytest.mark.parametrize("operation", ["backup", "restore"])
def test_the_library_refuses_a_level_outside_1_to_9_before_any_run(
    site: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    level: object,
    operation: str,
) -> None:
    called: list[Any] = []

    def driver(*args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        called.append((args, kwargs))

    monkeypatch.setattr(operations, f"drive_{operation}", driver)
    archive = tmp_path / "x.tar.gz"
    calls: dict[str, Callable[[], object]] = {
        "backup": lambda: api.backup(
            site,
            scope=BackupScope.FILES,
            target=archive,
            compression_level=level,  # type: ignore[arg-type]
        ),
        "restore": lambda: api.restore(
            site,
            archive=archive,
            mode=RestoreMode.FILES,
            overrides=OverrideSet(waived=frozenset(Override)),
            snapshot=SnapshotPolicy.TAKE,
            config_policy=ConfigPolicy.KEEP,
            compression_level=level,  # type: ignore[arg-type]
        ),
    }
    with pytest.raises(UsageError) as caught:
        calls[operation]()
    assert caught.value.arguments == ("compression_level",)
    assert called == []
