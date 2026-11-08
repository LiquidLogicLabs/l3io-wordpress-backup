"""BL-E006-007 (R-003) — concurrent Runs never carry each other's trace or span ids.

Several ``api.verify`` calls run at once on threads (the library API is
synchronous), each held inside its Run by a barrier until every Run is open,
under the in-memory OpenTelemetry SDK. Read back:

- each Run has exactly one ``l3io.wp.backup.run`` span, and its trace id is
  shared by no other Run's;
- every span of a trace has its parent inside that same trace, ending at the
  Run's own root;
- every log record carrying ``trace_id``/``span_id`` (``add_trace_ids``)
  names its own Run's trace and a span of that trace.

No database is needed: ``verify`` reads only the Archive.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
import structlog
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from structlog.testing import LogCapture

from l3io.wp.backup import api
from l3io.wp.backup.adapters.tracing import RUN_SPAN_NAME, add_trace_ids
from l3io.wp.backup.cli import verify as verify_mod
from tests.conftest import seal_archive
from tests.otel_sdk import otel_spans, parent_id, span_id  # noqa: F401 - fixture

RUNS = 6


def test_concurrent_runs_keep_their_own_trace_and_span_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    otel_spans: InMemorySpanExporter,  # noqa: F811
) -> None:
    archives = []
    for n in range(RUNS):
        work = tmp_path / f"run-{n}"
        work.mkdir()
        archives.append(seal_archive(work, None))
    barrier = threading.Barrier(RUNS, timeout=30)
    real = verify_mod.verify_archive_manifest

    def overlapping(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        barrier.wait()  # every Run is open, inside its phase span, at once
        return real(*args, **kwargs)

    monkeypatch.setattr(verify_mod, "verify_archive_manifest", overlapping)
    capture = LogCapture()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            add_trace_ids,
            capture,
        ]
    )
    try:
        with ThreadPoolExecutor(max_workers=RUNS) as pool:
            results = list(pool.map(api.verify, archives))
    finally:
        structlog.reset_defaults()
    assert all(r.passed for r in results)

    spans = otel_spans.get_finished_spans()
    roots = [s for s in spans if s.name == RUN_SPAN_NAME]
    assert len(roots) == RUNS
    trace_of_run: dict[str, int] = {}
    for root in roots:
        assert root.context is not None
        assert root.attributes is not None
        trace_of_run[str(root.attributes["l3io.run_id"])] = root.context.trace_id
    assert len(set(trace_of_run.values())) == RUNS, "two Runs share a trace"

    by_trace: dict[int, dict[int, Any]] = defaultdict(dict)
    for span in spans:
        assert span.context is not None
        by_trace[span.context.trace_id][span_id(span)] = span
    assert set(by_trace) == set(trace_of_run.values())
    for members in by_trace.values():
        assert len(members) > 1, "a Run with no phase span proves nothing"
        for span in members.values():
            parent = parent_id(span)
            if span.name == RUN_SPAN_NAME:
                assert parent is None or parent not in members
            else:
                assert parent in members, f"{span.name} parented outside its Run"

    traced = [e for e in capture.entries if "trace_id" in e]
    assert traced, "no record carried a trace id: the log check checked nothing"
    seen_runs = set()
    for event in traced:
        run_id = event.get("l3io_run_id") or event.get("run_id")
        assert run_id in trace_of_run, event
        trace_id = trace_of_run[str(run_id)]
        assert event["trace_id"] == f"{trace_id:032x}", event
        assert int(str(event["span_id"]), 16) in by_trace[trace_id], event
        seen_runs.add(run_id)
    assert seen_runs == set(trace_of_run)
