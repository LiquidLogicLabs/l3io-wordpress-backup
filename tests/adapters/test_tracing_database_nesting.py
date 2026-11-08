"""AD-24 — database's own spans nest under the phase span, by context alone.

The real ``WpDatabase.dump_to`` calls the real ``l3io.wp.database.dump``,
whose ``_observe`` opens ``l3io.wp.database.dump``; only what lies below it —
starting the client process — is a double, which fails the transfer. Run
inside ``adapters.phase.phase("DUMP")`` under ``OpenTelemetryTracer``, that
span is a child of the ``DUMP`` phase span, and this package opens no span
around the database call itself.

A failed database call exports its error's class and never its text, in
database's span or ours (BL-E006-010, database >= 0.3.3).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

pytest.importorskip("l3io.wp.database")

from l3io.wp.database import transfer
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode
from structlog.testing import capture_logs

from l3io.wp import database as wpdb
from l3io.wp.backup.adapters.database import WpDatabase
from l3io.wp.backup.adapters.phase import ACTIVE_TRACER, phase
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.adapters.tracing import OpenTelemetryTracer
from l3io.wp.backup.core.backup import BackupStep
from l3io.wp.backup.domain.database import DatabaseLocation
from l3io.wp.backup.domain.errors import DumpFailedError
from tests.otel_sdk import by_name, otel_spans, parent_id, span_id  # noqa: F401

LOCATION = DatabaseLocation(host="db.invalid", port=3306, name="wptest")


def _no_client(*args: object, **kwargs: object) -> object:
    """Below ``_observe``: the client process never starts."""
    msg = "the client could not start"
    raise wpdb.TransferError(msg, wpdb.Verdict("dump"), 1, "")


@pytest.fixture
def run(
    monkeypatch: pytest.MonkeyPatch,
    otel_spans: InMemorySpanExporter,  # noqa: F811 - installed before the root opens
) -> Iterator[SecretRegistry]:
    """An open root span with the tracer bound where ``phase()`` reads it."""
    del otel_spans
    monkeypatch.setattr(transfer, "_start_dump", _no_client)
    tracer, registry = OpenTelemetryTracer(), SecretRegistry()
    token = ACTIVE_TRACER.set((tracer, registry))
    try:
        with tracer.run_span(
            run_id="r-1",
            operation="backup",
            attributes={},
            command_args=None,
            secrets=registry,
        ):
            yield registry
    finally:
        ACTIVE_TRACER.reset(token)


def test_the_database_span_is_a_child_of_the_dump_phase_span(
    run: SecretRegistry,
    tmp_path: Path,
    otel_spans: InMemorySpanExporter,  # noqa: F811
) -> None:
    database = WpDatabase(wpdb.Explicit("wp", "not-a-real-secret"), secrets=run)
    with (
        capture_logs(),
        pytest.raises(DumpFailedError),
        phase(BackupStep.DUMP.value),
    ):
        database.dump_to(LOCATION, tmp_path / "dump.sql")
    spans = otel_spans.get_finished_spans()
    (dump,) = by_name(spans, "l3io.wp.database.dump")
    (step,) = by_name(spans, "l3io.wp.backup.dump")
    assert parent_id(dump) == span_id(step)


def test_no_backup_span_wraps_a_single_database_call(
    tmp_path: Path,
    run: SecretRegistry,
    otel_spans: InMemorySpanExporter,  # noqa: F811
) -> None:
    root = trace.get_current_span()
    assert root.is_recording()
    database = WpDatabase(wpdb.Explicit("wp", "not-a-real-secret"), secrets=run)
    with capture_logs(), pytest.raises(DumpFailedError):
        database.dump_to(LOCATION, tmp_path / "dump.sql")
    spans = otel_spans.get_finished_spans()
    (dump,) = by_name(spans, "l3io.wp.database.dump")
    assert parent_id(dump) == root.get_span_context().span_id
    assert by_name(spans, "l3io.wp.backup.") == []


def _span_text(span: ReadableSpan) -> list[str]:
    """Every exported string a span carries: attributes, events, status."""
    texts = [str(value) for value in (span.attributes or {}).values()]
    for event in span.events:
        texts.append(event.name)
        texts.extend(str(value) for value in (event.attributes or {}).values())
    texts.append(span.status.description or "")
    return texts


def _call_dump(database: WpDatabase, destination: Path) -> None:
    database.dump_to(LOCATION, destination)


def _call_check_dump(database: WpDatabase, destination: Path) -> None:
    del destination
    database.check_dump(LOCATION)


@pytest.mark.parametrize(
    ("call", "patched", "database_span"),
    [
        # Running: the span that outlives dump()'s call.
        (_call_dump, "_start_dump", "l3io.wp.database.dump"),
        # operation(): the span around one call.
        (_call_check_dump, "_dump_plan", "l3io.wp.database.check_dump"),
    ],
    ids=["dump", "check_dump"],
)
def test_no_span_carries_the_database_errors_text(
    call: Callable[[WpDatabase, Path], None],
    patched: str,
    database_span: str,
    run: SecretRegistry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    otel_spans: InMemorySpanExporter,  # noqa: F811
) -> None:
    """BL-E006-010: a failed database call exports its error's class, not its text.

    The sentinel stands for what a MySQL or database error can carry (a user,
    a host, SQL). Neither database's span nor ours may hold it, in an event
    (``record_exception``), an attribute, or the status description. Against
    database 0.3.2, whose spans record the exception, the event's
    ``exception.message`` holds it and this fails.
    """
    sentinel = f"db-error-sentinel-{uuid.uuid4().hex}"

    def failing(*args: object, **kwargs: object) -> object:
        raise wpdb.TransferError(sentinel, wpdb.Verdict("dump"), 1, sentinel)

    monkeypatch.setattr(transfer, patched, failing)
    database = WpDatabase(wpdb.Explicit("wp", "not-a-real-secret"), secrets=run)
    with (
        capture_logs(),
        pytest.raises(DumpFailedError),
        phase(BackupStep.DUMP.value),
    ):
        call(database, tmp_path / "dump.sql")
    spans = otel_spans.get_finished_spans()
    (failed,) = by_name(spans, database_span)
    # The guard's reach: the database span exists, failed and was examined.
    assert failed.status.status_code is StatusCode.ERROR
    assert by_name(spans, "l3io.wp.backup.dump")
    leaks = {
        span.name: text
        for span in spans
        for text in _span_text(span)
        if sentinel in text
    }
    assert leaks == {}
    # 0.3.3 names the failure by class only (BL-E006-010).
    assert (failed.attributes or {}).get("error.type") == "TransferError"
