"""AD-24 — ``OpenTelemetryTracer`` under an in-memory SDK (E006-S01-004).

A backup and a restore run through ``l3io.wp.backup.api`` inside a host span,
over Epic 2's and Epic 3's doubles, and the exported span tree is read back:

- exactly one ``l3io.wp.backup.run`` per Run, a child of the host's span;
- one phase span per ``phase_completed`` record, compared as multisets with
  the records the same Run logged — never with a hand-kept list, because
  phases nest (``ENCRYPT`` inside ``WRITE_ARCHIVE``; a Safety Snapshot's
  backup steps inside the restore's ``snapshot`` phase; E006 arch gate D6);
- the root's direct children, in start order, are the plan's phases;
- the span count does not grow with the file count;
- a failing phase ends its span and the root ``ERROR`` with the exception's
  ``exit_code``, and no span carries an exception event.

The adapter's own contract — no attribute work on a non-recording span, the
command line only through the Run's registry, ``l3io.*`` keys only (owner
decision O-2) — is checked on the adapter directly, each with a planted
violation.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode
from structlog.testing import capture_logs

from l3io.wp.backup import api
from l3io.wp.backup.adapters import tracing as tracing_mod
from l3io.wp.backup.adapters.noop import JOURNAL_PHASE
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.adapters.tracing import (
    RUN_SPAN_NAME,
    OpenTelemetryTracer,
)
from l3io.wp.backup.cli import restore as restore_mod
from l3io.wp.backup.core.backup import BackupStep
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.errors import DumpFailedError
from l3io.wp.backup.domain.restore import (
    ConfigPolicy,
    RestoreMode,
    RestorePhase,
    SnapshotPolicy,
    restores_database,
)
from l3io.wp.backup.ports.tracing import RunSpan, SpanAttributes
from tests.otel_sdk import (  # noqa: F401 - otel_spans is a fixture
    by_name,
    drop_everything,
    otel_spans,
    parent_id,
    span_id,
)
from tests.restore_root_rig import ALL_WAIVED, Rig
from tests.unit.cli.conftest import (  # noqa: F401 - fixtures, re-registered here
    FakeDatabase,
    fake_database,
    site,
    workdir,
)

PREFIX = "l3io.wp.backup."
#: database's passphrase backend; ``binder`` needs no external ``age`` (A17).
BACKEND_ENV = "L3IO_AGE_PASSPHRASE_BACKEND"
PASSPHRASE = "tracing-test-passphrase-0123456789"

_host = trace.get_tracer("tests.host")


@pytest.fixture(autouse=True)
def _no_passphrase(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("L3IO_WP_BACKUP_PASSPHRASE", raising=False)
    monkeypatch.setenv(BACKEND_ENV, "binder")


# --------------------------------------------------------------------------
# Reading the tree
# --------------------------------------------------------------------------


def _ours(spans: Sequence[ReadableSpan]) -> list[ReadableSpan]:
    return by_name(spans, PREFIX)


def _roots(spans: Sequence[ReadableSpan]) -> list[ReadableSpan]:
    return [s for s in spans if s.name == RUN_SPAN_NAME]


def _phases(spans: Sequence[ReadableSpan]) -> list[ReadableSpan]:
    return [s for s in _ours(spans) if s.name != RUN_SPAN_NAME]


def _phase_of(span: ReadableSpan) -> str:
    assert span.attributes is not None
    return str(span.attributes["l3io.phase"])


def _recorded_phases(logs: list[dict[str, Any]]) -> Counter[str]:
    """The ``phase()`` records. A record with a ``step`` is a sub-step logged
    inside the open phase (``adapters/staging.py``'s cutover, inside
    ``load-dump``), not a phase of its own; its time is in that phase's span."""
    return Counter(
        str(e["phase"])
        for e in logs
        if e["event"] == "phase_completed" and "step" not in e
    )


def _span_phases(spans: Sequence[ReadableSpan]) -> Counter[str]:
    return Counter(_phase_of(s) for s in _phases(spans))


def _ancestors(span: ReadableSpan, spans: Sequence[ReadableSpan]) -> list[int]:
    by_id = {span_id(s): s for s in spans}
    found: list[int] = []
    parent = parent_id(span)
    while parent is not None:
        found.append(parent)
        above = by_id.get(parent)
        parent = None if above is None else parent_id(above)
    return found


def _check_tree(
    spans: Sequence[ReadableSpan], logs: list[dict[str, Any]], host: int
) -> ReadableSpan:
    """One root under ``host``; the phase spans are the records; all under it."""
    (root,) = _roots(spans)
    assert parent_id(root) == host
    assert _span_phases(spans) == _recorded_phases(logs)
    for span in _phases(spans):
        assert span_id(root) in _ancestors(span, spans), span.name
        assert span.name == PREFIX + _phase_of(span).lower()
    return root


def _children(root: ReadableSpan, spans: Sequence[ReadableSpan]) -> list[str]:
    return [_phase_of(s) for s in _phases(spans) if parent_id(s) == span_id(root)]


def _forbidden_keys(spans: Sequence[ReadableSpan]) -> list[str]:
    """Owner decision O-2: no ``wpbackup2.*`` key and no bare ``dry_run``."""
    return sorted(
        key
        for s in spans
        for key in (s.attributes or {})
        if key.startswith("wpbackup2.") or key == "dry_run"
    )


def _no_exception_events(spans: Sequence[ReadableSpan]) -> None:
    for span in _ours(spans):
        assert [e.name for e in span.events] == [], span.name


def _spy_root_attributes(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """What ``open_run`` hands the tracer — the expected root keys, not a literal."""
    seen: list[dict[str, Any]] = []
    real = OpenTelemetryTracer.run_span

    def spy(
        self: OpenTelemetryTracer,
        **kwargs: Any,  # noqa: ANN401 - forwarded unchanged
    ) -> AbstractContextManager[RunSpan]:
        seen.append(dict(kwargs["attributes"]))
        return real(self, **kwargs)

    monkeypatch.setattr(OpenTelemetryTracer, "run_span", spy)
    return seen


# --------------------------------------------------------------------------
# Backup
# --------------------------------------------------------------------------


@pytest.mark.usefixtures("fake_database")
@pytest.mark.parametrize("encrypt", [False, True], ids=["plain", "encrypted"])
def test_a_backup_is_one_root_with_one_span_per_phase_record(
    encrypt: bool,
    site: Path,  # noqa: F811 - the fixture
    tmp_path: Path,
    otel_spans: InMemorySpanExporter,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attributes = _spy_root_attributes(monkeypatch)
    with capture_logs() as logs, _host.start_as_current_span("host") as host:
        result = api.backup(
            site,
            scope=BackupScope.ALL,
            target=tmp_path / "out.tar.gz",
            encrypt=encrypt,
            passphrase=PASSPHRASE if encrypt else None,
        )
    spans = otel_spans.get_finished_spans()
    root = _check_tree(spans, logs, span_id(host))  # type: ignore[arg-type]
    if encrypt:
        (encrypt_span,) = [
            s for s in _phases(spans) if _phase_of(s) == BackupStep.ENCRYPT.value
        ]
        (write,) = [
            s for s in _phases(spans) if _phase_of(s) == BackupStep.WRITE_ARCHIVE.value
        ]
        assert parent_id(encrypt_span) == span_id(write)
    (run_started,) = [e for e in logs if e["event"] == "run_started"]
    (expected,) = attributes
    assert root.attributes is not None
    assert {k: root.attributes[k] for k in expected} == expected
    assert root.attributes["l3io.run_id"] == run_started["run_id"]
    assert root.attributes["db.name"] == "wptest"
    assert root.attributes["l3io.archive.bytes"] == result.bytes
    assert root.attributes["process.exit.code"] == 0
    assert "process.command_args" not in root.attributes
    assert "error.type" not in root.attributes
    assert root.status.status_code is StatusCode.UNSET
    assert _forbidden_keys(spans) == []
    _no_exception_events(spans)


def test_dropping_one_span_breaks_the_comparison(
    site: Path,  # noqa: F811 - the fixture
    tmp_path: Path,
    fake_database: FakeDatabase,  # noqa: F811
    otel_spans: InMemorySpanExporter,  # noqa: F811
) -> None:
    del fake_database
    with capture_logs() as logs, _host.start_as_current_span("host"):
        api.backup(site, scope=BackupScope.ALL, target=tmp_path / "o.tar.gz")
    spans = list(otel_spans.get_finished_spans())
    planted = [s for s in spans if s is not _phases(spans)[-1]]
    assert _span_phases(spans) == _recorded_phases(logs)
    assert _span_phases(planted) != _recorded_phases(logs)


def _many(root: Path, count: int) -> None:
    content = root / "wp-content"
    content.mkdir(exist_ok=True)
    for n in range(count):
        (content / f"f{n:04d}.txt").write_text(str(n), encoding="utf-8")


@pytest.mark.usefixtures("fake_database")
def test_the_span_count_does_not_follow_the_file_count(
    site: Path,  # noqa: F811 - the fixture
    tmp_path: Path,
    otel_spans: InMemorySpanExporter,  # noqa: F811
) -> None:
    counts: list[int] = []
    for files in (1, 500):
        _many(site, files)
        otel_spans.clear()
        with capture_logs():
            api.backup(site, scope=BackupScope.ALL, target=tmp_path / f"{files}.tar.gz")
        counts.append(len(_ours(otel_spans.get_finished_spans())))
    assert counts[0] == counts[1]
    assert counts[0] > 1


def test_a_failing_phase_ends_its_span_and_the_root_in_error(
    site: Path,  # noqa: F811 - the fixture
    tmp_path: Path,
    fake_database: FakeDatabase,  # noqa: F811
    otel_spans: InMemorySpanExporter,  # noqa: F811
) -> None:
    fake_database.fail = True
    with (
        capture_logs() as logs,
        _host.start_as_current_span("host") as host,
        pytest.raises(DumpFailedError) as caught,
    ):
        api.backup(site, scope=BackupScope.ALL, target=tmp_path / "out.tar.gz")
    spans = otel_spans.get_finished_spans()
    root = _check_tree(spans, logs, span_id(host))  # type: ignore[arg-type]
    code = int(caught.value.exit_code)
    (dump,) = [s for s in _phases(spans) if _phase_of(s) == BackupStep.DUMP.value]
    for span in (dump, root):
        assert span.status.status_code is StatusCode.ERROR, span.name
        assert span.attributes is not None
        assert span.attributes["error.type"] == "DumpFailedError"
        assert span.attributes["process.exit.code"] == code
    assert dump.status.description == str(caught.value)
    _no_exception_events(spans)


@pytest.mark.usefixtures("fake_database")
def test_a_tracer_that_raises_is_logged_once_and_changes_no_outcome(
    site: Path,  # noqa: F811 - the fixture
    tmp_path: Path,
    otel_spans: InMemorySpanExporter,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenSdk:
        def start_as_current_span(self, *args: object, **kwargs: object) -> None:
            msg = "the host's SDK broke"
            raise RuntimeError(msg)

    monkeypatch.setattr(tracing_mod, "_tracer", BrokenSdk)
    with capture_logs() as logs:
        result = api.backup(site, scope=BackupScope.ALL, target=tmp_path / "o.tar.gz")
    assert result.performed
    failed = [e for e in logs if e["event"] == "tracer_failed"]
    assert len(failed) == 1
    assert failed[0]["tracer"] == "OpenTelemetryTracer"
    assert [e for e in logs if e["event"] == "tracing_error"] == []
    assert _ours(otel_spans.get_finished_spans()) == []


# --------------------------------------------------------------------------
# Restore — every Mode, scope from the enum
# --------------------------------------------------------------------------


def _restore(rig: Rig, mode: RestoreMode, *, dry_run: bool) -> None:
    api.restore(
        rig.site_root,
        archive=rig.archive_path,
        mode=mode,
        overrides=ALL_WAIVED,
        snapshot=SnapshotPolicy.TAKE,
        config_policy=ConfigPolicy.KEEP,
        snapshot_dir=rig.work / "snapshots",
        dry_run=dry_run,
    )


@pytest.fixture
def real_rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    """The restore root's rig, with the real executor and the real snapshot backup."""
    backup = restore_mod.backup
    rig = Rig(tmp_path, monkeypatch)
    rig.real_executor()
    monkeypatch.setattr(restore_mod, "backup", backup)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    return rig


def _snapshot_backup_nests(spans: Sequence[ReadableSpan]) -> None:
    """A Safety Snapshot's backup steps sit under the restore's snapshot span."""
    (snapshot,) = [
        s for s in _phases(spans) if _phase_of(s) == RestorePhase.SNAPSHOT.value
    ]
    steps = {step.value for step in BackupStep}
    nested = [s for s in _phases(spans) if _phase_of(s) in steps]
    assert nested
    for span in nested:
        assert span_id(snapshot) in _ancestors(span, spans), span.name


@pytest.mark.parametrize("mode", list(RestoreMode), ids=lambda m: m.value)
def test_a_restore_dry_run_is_one_root_with_the_plan_as_its_children(
    mode: RestoreMode,
    real_rig: Rig,
    otel_spans: InMemorySpanExporter,  # noqa: F811
) -> None:
    with capture_logs() as logs, _host.start_as_current_span("host") as host:
        _restore(real_rig, mode, dry_run=True)
    spans = otel_spans.get_finished_spans()
    root = _check_tree(spans, logs, span_id(host))  # type: ignore[arg-type]
    plan = real_rig.plans[-1]
    # A Dry Run's no-op journal logs its own phase between the plan's (AD-4).
    assert [p for p in _children(root, spans) if p != JOURNAL_PHASE] == [
        p.value for p in plan.phases
    ]
    _snapshot_backup_nests(spans)
    assert _forbidden_keys(spans) == []
    _no_exception_events(spans)


@pytest.mark.parametrize(
    # The rig's database double cannot load a Dump: the database Modes are
    # driven as Dry Runs above.
    "mode",
    [m for m in RestoreMode if not restores_database(m)],
    ids=lambda m: m.value,
)
def test_a_real_file_restore_has_exactly_the_plan_as_root_children(
    mode: RestoreMode,
    real_rig: Rig,
    otel_spans: InMemorySpanExporter,  # noqa: F811
) -> None:
    with capture_logs() as logs, _host.start_as_current_span("host") as host:
        _restore(real_rig, mode, dry_run=False)
    spans = otel_spans.get_finished_spans()
    root = _check_tree(spans, logs, span_id(host))  # type: ignore[arg-type]
    assert _children(root, spans) == [p.value for p in real_rig.plans[-1].phases]
    _snapshot_backup_nests(spans)


# --------------------------------------------------------------------------
# The adapter directly: cost, the command line, keys
# --------------------------------------------------------------------------

SENTINEL = "sentinel-admin-pw-0123456789abcdef"
ARGV = ("restore", "--admin-password", SENTINEL, "--site", "/srv/www")


class SpyRegistry:
    """The Run's registry, counting every redaction the tracer asks for."""

    def __init__(self) -> None:
        self.inner = SecretRegistry()
        self.inner.register(SENTINEL, source="--admin-password")
        self.calls: list[str] = []

    def redact(self, text: str) -> str:
        self.calls.append("redact")
        return self.inner.redact(text)

    def redact_argv(self, argv: Sequence[str]) -> list[str]:
        self.calls.append("redact_argv")
        return self.inner.redact_argv(argv)


def _failing_phase(tracer: OpenTelemetryTracer, secrets: SpyRegistry) -> None:
    with tracer.phase_span("EXTRACT", secrets=secrets) as phase:
        phase.set_outcome(exit_code=1, archive_bytes=3)
        msg = f"failed near {SENTINEL}"
        raise RuntimeError(msg)


def _drive(tracer: OpenTelemetryTracer, secrets: SpyRegistry) -> None:
    attributes: SpanAttributes = {"l3io.operation": "restore"}
    with tracer.run_span(
        run_id="r-1",
        operation="restore",
        attributes=attributes,
        command_args=ARGV,
        secrets=secrets,
    ) as run:
        run.set_attributes(db_name=f"db-{SENTINEL}", archive_bytes=7)
        with pytest.raises(RuntimeError):
            _failing_phase(tracer, secrets)
        run.set_outcome(exit_code=1, error_type="RuntimeError")


def test_a_non_recording_span_costs_no_redaction(
    otel_spans: InMemorySpanExporter,  # noqa: F811
) -> None:
    drop_everything()
    secrets = SpyRegistry()
    _drive(OpenTelemetryTracer(), secrets)
    assert secrets.calls == []
    assert otel_spans.get_finished_spans() == ()


def _leaks(spans: Sequence[ReadableSpan]) -> list[str]:
    """Every place the sentinel survives on an exported span."""
    found: list[str] = []
    for span in spans:
        texts: dict[str, object] = {
            "name": span.name,
            "status": span.status.description,
            **{f"attr {k}": v for k, v in (span.attributes or {}).items()},
            **{f"event {e.name}": dict(e.attributes or {}) for e in span.events},
        }
        found += [f"{span.name}: {k}" for k, v in texts.items() if SENTINEL in str(v)]
    return found


def test_a_recording_span_states_the_command_line_only_redacted(
    otel_spans: InMemorySpanExporter,  # noqa: F811
) -> None:
    secrets = SpyRegistry()
    _drive(OpenTelemetryTracer(), secrets)
    spans = otel_spans.get_finished_spans()
    (root,) = _roots(spans)
    assert root.attributes is not None
    assert list(root.attributes["process.command_args"]) == secrets.inner.redact_argv(  # type: ignore[arg-type]
        ARGV
    )
    assert root.attributes["process.executable.name"] == "wpbackup2"
    assert root.attributes["l3io.run_id"] == "r-1"
    assert root.attributes["l3io.archive.bytes"] == 7
    (phase,) = _phases(spans)
    assert phase.attributes is not None
    assert phase.attributes["error.type"] == "RuntimeError"
    assert phase.attributes["process.exit.code"] == 1
    assert phase.status.status_code is StatusCode.ERROR
    assert phase.status.description == secrets.inner.redact(f"failed near {SENTINEL}")
    assert root.status.status_code is StatusCode.ERROR
    assert "redact_argv" in secrets.calls
    assert _leaks(spans) == []
    _no_exception_events(spans)


class _RawArgv(OpenTelemetryTracer):
    """A planted adapter that puts the raw command line on the span."""

    __slots__ = ()

    @contextmanager
    def run_span(  # type: ignore[override]
        self,
        *,
        command_args: Sequence[str] | None,
        **kwargs: Any,  # noqa: ANN401
    ) -> Iterator[RunSpan]:
        with super().run_span(command_args=command_args, **kwargs) as span:
            trace.get_current_span().set_attribute(
                "process.command_args", list(command_args or ())
            )
            yield span


def test_a_planted_raw_command_line_is_caught(
    otel_spans: InMemorySpanExporter,  # noqa: F811
) -> None:
    _drive(_RawArgv(), SpyRegistry())
    assert _leaks(otel_spans.get_finished_spans()) == [
        f"{RUN_SPAN_NAME}: attr process.command_args"
    ]


def test_a_planted_legacy_key_is_caught(
    otel_spans: InMemorySpanExporter,  # noqa: F811
) -> None:
    with _host.start_as_current_span(RUN_SPAN_NAME) as span:
        span.set_attributes({"wpbackup2.run_id": "x", "dry_run": True, "l3io.x": 1})
    assert _forbidden_keys(otel_spans.get_finished_spans()) == [
        "dry_run",
        "wpbackup2.run_id",
    ]


@pytest.mark.parametrize(
    "interrupt",
    [KeyboardInterrupt, SystemExit],
    ids=["KeyboardInterrupt", "SystemExit"],
)
def test_every_span_ends_on_an_interrupt(
    interrupt: Callable[[], BaseException],
    otel_spans: InMemorySpanExporter,  # noqa: F811
) -> None:
    tracer, secrets = OpenTelemetryTracer(), SecretRegistry()
    attributes: Mapping[str, str | int | bool] = {}
    with (
        pytest.raises(interrupt),  # type: ignore[call-overload]
        tracer.run_span(
            run_id="r",
            operation="backup",
            attributes=attributes,
            command_args=None,
            secrets=secrets,
        ),
        tracer.phase_span("DUMP", secrets=secrets),
    ):
        raise interrupt()
    spans = otel_spans.get_finished_spans()
    assert sorted(s.name for s in spans) == [PREFIX + "dump", RUN_SPAN_NAME]
    assert all(s.status.status_code is StatusCode.ERROR for s in spans)
