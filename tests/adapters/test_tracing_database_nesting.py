"""AD-24 — database's own spans nest under the phase span, by context alone.

The real ``WpDatabase.dump_to`` calls the real ``l3io.wp.database.dump``,
whose ``_observe`` opens ``l3io.wp.database.dump``; only what lies below it —
starting the client process — is a double, which fails the transfer. Run
inside ``adapters.phase.phase("DUMP")`` under ``OpenTelemetryTracer``, that
span is a child of the ``DUMP`` phase span, and this package opens no span
around the database call itself.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("l3io.wp.database")

from l3io.wp.database import transfer
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
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
