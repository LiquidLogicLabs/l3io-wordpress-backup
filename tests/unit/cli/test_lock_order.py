"""AD-16/AD-19/ADR-0016 — the Site Lock is taken before the first read of Site state.

One journal, written by a recording ``Tracer``, a recording ``SiteLock``, the
``wp-config.php`` locator and reader, the credential source builder and
``RunContext.bind_adapters``. Through ``drive_backup`` (what the CLI and
``api/`` both call) the order is: root span entered, lock acquired,
``wp-config.php`` located and read, the credential source built,
``bind_adapters``, the first phase; the lock is released after the last phase,
on success and on a failure. A refused lock is a failed root span — ``LOCKED``
and ``SiteLockedError`` — and nothing after it runs.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from l3io.wp.backup.cli import backup as backup_cli
from l3io.wp.backup.cli.context import RunContext
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.errors import DumpFailedError, SiteLockedError
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.ports.tracing import PhaseSpan, Redactor, RunSpan
from tests.unit.cli.conftest import FakeDatabase, RecordingSiteLock


class JournalTracer:
    """A ``Tracer``/``RunSpan``/``PhaseSpan`` writing names into ``events``."""

    def __init__(self, events: list[object]) -> None:
        self.events = events

    @contextmanager
    def run_span(
        self,
        *,
        run_id: str,
        operation: str,
        attributes: Mapping[str, str | int | bool],
        command_args: Sequence[str] | None,
        secrets: Redactor,
    ) -> Iterator[RunSpan]:
        self.events.append("run_span")
        try:
            yield self
        finally:
            self.events.append("run_span_exit")

    @contextmanager
    def phase_span(self, phase: str, *, secrets: Redactor) -> Iterator[PhaseSpan]:
        self.events.append(f"phase:{phase}")
        try:
            yield self  # type: ignore[misc]
        finally:
            self.events.append(f"phase_exit:{phase}")

    def set_attributes(
        self, *, db_name: str | None = None, archive_bytes: int | None = None
    ) -> None:
        pass

    def set_outcome(self, **outcome: object) -> None:
        if "error_type" in outcome:
            self.events.append(
                ("run_outcome", outcome["exit_code"], outcome["error_type"])
            )


def _recording(
    monkeypatch: pytest.MonkeyPatch,
    events: list[object],
    owner: object,
    name: str,
    label: str,
) -> None:
    inner: Callable[..., object] = getattr(owner, name)

    def call(*args: object, **kwargs: object) -> object:
        events.append(label)
        return inner(*args, **kwargs)

    monkeypatch.setattr(owner, name, call)


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    journal: list[object] = []
    _recording(monkeypatch, journal, backup_cli, "locate_wp_config", "locate_wp_config")
    _recording(
        monkeypatch,
        journal,
        backup_cli,
        "site_descriptor_from_wp_config",
        "read_wp_config",
    )
    _recording(
        monkeypatch, journal, backup_cli, "wordpress_source", "credential_source"
    )
    _recording(monkeypatch, journal, RunContext, "bind_adapters", "bind_adapters")
    return journal


def _drive(
    site: Path, final: Path, events: list[object], lock: RecordingSiteLock
) -> backup_cli.BackupResult:
    request = backup_cli.BackupRequest(
        site_root=site,
        scope=BackupScope.ALL,
        target=backup_cli.storage_target_from_cli(str(final)),
        wp_config=None,
        credentials=None,
        cipher=None,
    )
    with capture_logs():
        return backup_cli.drive_backup(
            request, ctx=None, tracer=JournalTracer(events), site_lock=lock
        )


def _labels(events: list[object]) -> list[object]:
    return [
        e[0] if isinstance(e, tuple) and e[0].startswith("lock.") else e for e in events
    ]


def test_the_lock_is_taken_first_and_released_last(
    site: Path, tmp_path: Path, events: list[object], fake_database: FakeDatabase
) -> None:
    lock = RecordingSiteLock(events)
    _drive(site, tmp_path / "out.tar.gz", events, lock)
    labels = _labels(events)
    assert labels[:7] == [
        "run_span",
        "lock.acquire",
        "locate_wp_config",
        "read_wp_config",
        "credential_source",
        "bind_adapters",
        "phase:DUMP",
    ]
    phases = [i for i, e in enumerate(labels) if str(e).startswith("phase_exit:")]
    release = labels.index("lock.release")
    assert phases, "no phase ran"
    assert release > max(phases)
    assert labels[release + 1 :] == [("run_outcome", 0, None), "run_span_exit"]
    acquired = events[labels.index("lock.acquire")]
    assert isinstance(acquired, tuple)
    assert acquired[1] == site.resolve()


def test_the_lock_is_released_on_a_failure(
    site: Path, tmp_path: Path, events: list[object], fake_database: FakeDatabase
) -> None:
    fake_database.fail = True
    lock = RecordingSiteLock(events)
    with pytest.raises(DumpFailedError):
        _drive(site, tmp_path / "out.tar.gz", events, lock)
    labels = _labels(events)
    assert labels.count("lock.acquire") == labels.count("lock.release") == 1
    assert labels.index("lock.release") > labels.index("phase_exit:DUMP")
    assert labels[-2:] == [("run_outcome", 1, "DumpFailedError"), "run_span_exit"]


def test_a_refused_lock_is_a_failed_root_span_and_reads_nothing(
    site: Path, tmp_path: Path, events: list[object], fake_database: FakeDatabase
) -> None:
    lock = RecordingSiteLock(events)
    lock.refuse = True
    with pytest.raises(SiteLockedError):
        _drive(site, tmp_path / "out.tar.gz", events, lock)
    assert _labels(events) == [
        "run_span",
        "lock.acquire",
        ("run_outcome", int(ExitCode.LOCKED), "SiteLockedError"),
        "run_span_exit",
    ]
    assert fake_database.checks == []
    assert fake_database.calls == []
    assert not (tmp_path / "out.tar.gz").exists()


def test_a_dry_run_takes_the_lock_for_real(
    site: Path, tmp_path: Path, events: list[object], fake_database: FakeDatabase
) -> None:
    lock = RecordingSiteLock(events)
    request = backup_cli.BackupRequest(
        site_root=site,
        scope=BackupScope.ALL,
        target=backup_cli.storage_target_from_cli(str(tmp_path / "out.tar.gz")),
        wp_config=None,
        credentials=None,
        cipher=None,
        dry_run=True,
    )
    with capture_logs():
        backup_cli.drive_backup(request, ctx=None, site_lock=lock)
    labels = _labels(events)
    assert labels.count("lock.acquire") == labels.count("lock.release") == 1
    assert labels.index("lock.acquire") < labels.index("locate_wp_config")
