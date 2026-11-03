"""AD-24 — the tracing hooks, observed with a recording ``Tracer``.

Inert in the CLI (``NoopTracer``); Epic 6's ``api/`` passes a real one. These
cases pin what that tracer will be told, and that it can never change an
outcome.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path

import pytest
from structlog.testing import capture_logs
from structlog.typing import EventDict, WrappedLogger

from l3io.wp.backup.adapters.noop import NoopTracer
from l3io.wp.backup.adapters.phase import ACTIVE_TRACER, phase
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.cli import backup as backup_cli
from l3io.wp.backup.cli.context import join_or_open, open_run
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.errors import DumpFailedError
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.ports.tracing import PhaseSpan, Redactor, RunSpan, Tracer
from tests.unit.cli.conftest import FakeDatabase

pytestmark = pytest.mark.usefixtures("fake_database")


class Recording:
    """A ``Tracer``, ``RunSpan`` and ``PhaseSpan`` that writes to one journal."""

    def __init__(self, journal: list[tuple[object, ...]], fail: str = "") -> None:
        self.journal = journal
        self.fail = fail
        self.redactors: list[Redactor] = []

    def _maybe_fail(self, call: str) -> None:
        if call == self.fail:
            msg = f"tracer broke in {call}"
            raise RuntimeError(msg)

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
        self._maybe_fail("run_span")
        self.redactors.append(secrets)
        self.journal.append(
            ("run_span", run_id, operation, dict(attributes), command_args)
        )
        yield self
        self.journal.append(("run_span_exit",))
        self._maybe_fail("run_span_exit")

    @contextmanager
    def phase_span(self, phase: str, *, secrets: Redactor) -> Iterator[PhaseSpan]:
        self._maybe_fail("phase_span")
        self.redactors.append(secrets)
        self.journal.append(("phase_span", phase))
        yield _PhaseSpan(self, phase)
        self.journal.append(("phase_span_exit", phase))
        self._maybe_fail("phase_span_exit")

    def set_attributes(
        self, *, db_name: str | None = None, archive_bytes: int | None = None
    ) -> None:
        self._maybe_fail("set_attributes")
        self.journal.append(("set_attributes", db_name, archive_bytes))

    def set_outcome(self, *, exit_code: int, error_type: str | None) -> None:
        self._maybe_fail("set_outcome")
        self.journal.append(("run_outcome", exit_code, error_type))


class _PhaseSpan:
    def __init__(self, owner: Recording, name: str) -> None:
        self.owner, self.name = owner, name

    def set_outcome(self, *, exit_code: int, archive_bytes: int | None) -> None:
        self.owner._maybe_fail("phase_outcome")  # noqa: SLF001
        self.owner.journal.append(
            ("phase_outcome", self.name, exit_code, archive_bytes)
        )


def _request(site: Path, final: Path, scope: BackupScope) -> backup_cli.BackupRequest:
    return backup_cli.BackupRequest(
        site_root=site,
        scope=scope,
        target=backup_cli.storage_target_from_cli(str(final)),
        wp_config=None,
        credentials=None,
        cipher=None,
    )


def _journaling(journal: list[tuple[object, ...]]) -> object:
    def processor(_l: WrappedLogger, _m: str, event_dict: EventDict) -> EventDict:
        journal.append(("log", event_dict["event"], event_dict.get("phase")))
        return event_dict

    return processor


def _structural(journal: list[tuple[object, ...]]) -> list[tuple[object, ...]]:
    return [e for e in journal if e[0] in {"run_span", "run_span_exit", "phase_span"}]


def test_the_tracer_is_told_everything_ad24_names(site: Path, tmp_path: Path) -> None:
    journal: list[tuple[object, ...]] = []
    tracer = Recording(journal)
    registry = SecretRegistry()
    with capture_logs(processors=[_journaling(journal)]):  # type: ignore[list-item]
        result = backup_cli.drive_backup(
            _request(site, tmp_path / "a.tar.gz", BackupScope.ALL),
            ctx=None,
            tracer=tracer,
            secrets=registry,
        )
    runs = [e for e in journal if e[0] == "run_span"]
    assert len(runs) == 1
    attributes = {"l3io.operation": "backup", "l3io.dry_run": False}
    assert runs[0][2:] == ("backup", attributes, None)  # no command line reaches a span
    assert all(r is registry for r in tracer.redactors)
    assert len(tracer.redactors) > 1

    # db.name after the config read; archive.bytes after publishing.
    events = [e[1] for e in journal if e[0] == "log"]
    attrs = [e for e in journal if e[0] == "set_attributes"]
    assert attrs == [
        ("set_attributes", "wptest", None),
        ("set_attributes", None, result.bytes),
    ]
    first_attr = journal.index(attrs[0])
    located = journal.index(("log", "wp_config_located", None))
    assert located < first_attr
    published = journal.index(("log", "archive_published", None))
    assert published < journal.index(attrs[1])

    # One phase span per phase_completed, same names, nested in the run span.
    phases = [e[1] for e in journal if e[0] == "phase_span"]
    completed = [e[2] for e in journal if e[0] == "log" and e[1] == "phase_completed"]
    assert phases == completed == ["DUMP", "COLLECT_SITE_ROOT", "WRITE_ARCHIVE",
                                   "VERIFY", "PUBLISH"]  # fmt: skip
    structure = _structural(journal)
    assert structure[0][0] == "run_span"
    assert structure[-1] == ("run_span_exit",)
    outcomes = [e for e in journal if e[0] == "phase_outcome"]
    assert [(e[1], e[2]) for e in outcomes] == [(p, 0) for p in phases]
    assert ("run_outcome", 0, None) in journal
    assert "run_finished" in events
    assert journal.index(("log", "run_finished", None)) < journal.index(
        ("run_span_exit",)
    )


def test_a_joined_run_opens_no_span(site: Path, tmp_path: Path, workdir: Path) -> None:
    journal: list[tuple[object, ...]] = []
    tracer = Recording(journal)
    with open_run(operation="restore", tracer=tracer) as ctx:
        with join_or_open(ctx, operation="backup", tracer=Recording([])) as handle:
            assert handle.opened is False
        assert len([e for e in journal if e[0] == "run_span"]) == 1
        assert ctx.tracer is tracer


def test_a_failed_run_reports_its_exit_code_and_error_type(
    site: Path, tmp_path: Path, fake_database: FakeDatabase
) -> None:
    fake_database.fail = True
    journal: list[tuple[object, ...]] = []
    with pytest.raises(DumpFailedError):
        backup_cli.drive_backup(
            _request(site, tmp_path / "a.tar.gz", BackupScope.DATABASE),
            ctx=None,
            tracer=Recording(journal),
        )
    assert ("run_outcome", 1, "DumpFailedError") in journal
    assert ("phase_outcome", "DUMP", 1, 0) in journal


FAILURES = [
    "run_span",
    "phase_span",
    "set_attributes",
    "set_outcome",
    "phase_outcome",
    "phase_span_exit",
    "run_span_exit",
]


@pytest.mark.parametrize("failing", FAILURES)
def test_a_raising_tracer_changes_no_outcome(
    site: Path, tmp_path: Path, failing: str
) -> None:
    final = tmp_path / "a.tar.gz"
    journal: list[tuple[object, ...]] = []
    with capture_logs() as logs:
        result = backup_cli.drive_backup(
            _request(site, final, BackupScope.ALL),
            ctx=None,
            tracer=Recording(journal, fail=failing),
        )
    assert result.performed
    # Every span that was entered is still exited, so a real tracer ends it.
    entered = [e[1] for e in journal if e[0] == "phase_span"]
    exited = [e[1] for e in journal if e[0] == "phase_span_exit"]
    assert entered == exited
    if failing != "run_span":
        assert ("run_span_exit",) in journal
    assert final.is_file()
    failed = [e for e in logs if e["event"] == "tracer_failed"]
    assert len(failed) == 1
    assert failed[0]["log_level"] == "warning"
    assert failed[0]["tracer"] == "Recording"
    (finished,) = [e for e in logs if e["event"] == "run_finished"]
    assert finished["exit_code"] == 0


def test_a_raising_tracer_does_not_mask_a_failure(
    site: Path, tmp_path: Path, fake_database: FakeDatabase
) -> None:
    fake_database.fail = True
    with capture_logs() as logs, pytest.raises(DumpFailedError):
        backup_cli.drive_backup(
            _request(site, tmp_path / "a.tar.gz", BackupScope.DATABASE),
            ctx=None,
            tracer=Recording([], fail="set_outcome"),
        )
    (finished,) = [e for e in logs if e["event"] == "run_finished"]
    assert finished["exit_code"] == 1


def test_a_failed_verify_finishes_its_run_non_zero(
    site: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from l3io.wp.backup.cli import main
    from l3io.wp.backup.cli import verify as verify_cli

    corrupt = tmp_path / "corrupt.tar.gz"
    corrupt.write_bytes(b"not an archive at all" * 64)
    journal: list[tuple[object, ...]] = []
    with capture_logs() as logs:
        result = verify_cli.drive_verify(
            verify_cli.VerifyRequest(
                archive=backup_cli.storage_target_from_cli(str(corrupt))
            ),
            ctx=None,
            tracer=Recording(journal),
        )
    assert not result.passed
    (finished,) = [e for e in logs if e["event"] == "run_finished"]
    code = int(ExitCode.VERIFICATION_FAILED)
    assert finished["exit_code"] == code
    assert finished["exit_reason"] == "VERIFICATION_FAILED"
    assert finished["error"] == "VerificationFailedError"
    assert ("run_outcome", code, "VerificationFailedError") in journal
    assert main(["verify", "--archive", str(corrupt)]) == code
    capsys.readouterr()


def test_outside_a_run_a_phase_uses_the_noop_tracer() -> None:
    assert ACTIVE_TRACER.get() is None
    with capture_logs() as logs, phase("X") as record:
        record.bytes = 3
    assert logs[0]["bytes"] == 3


def test_the_noop_tracer_satisfies_every_port() -> None:
    tracer: Tracer = NoopTracer()
    with tracer.run_span(
        run_id="r", operation="o", attributes={}, command_args=None,
        secrets=SecretRegistry(),
    ) as span:  # fmt: skip
        span.set_attributes(db_name="d")
        span.set_outcome(exit_code=0, error_type=None)
    with tracer.phase_span("p", secrets=SecretRegistry()) as phase_span:
        phase_span.set_outcome(exit_code=0, archive_bytes=None)
