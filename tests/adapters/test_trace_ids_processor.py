"""AD-24 — ``add_trace_ids``: flat ids while a span records, nothing otherwise.

The rule is ``is_recording()``, not "a valid span context exists": a span an
``ALWAYS_OFF`` sampler dropped has a valid context and still adds no key.
The slot is ``configure_logging``'s ``trace_processors``; nothing fills it by
default (E006 arch gate D4).
"""

from __future__ import annotations

import json
import re

import pytest
import structlog
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from structlog.typing import EventDict

from l3io.wp.backup import api
from l3io.wp.backup.adapters.tracing import add_trace_ids
from l3io.wp.backup.cli.logging import configure_logging
from tests.otel_sdk import drop_everything, otel_spans  # noqa: F401 - fixture

TRACE_ID = re.compile(r"[0-9a-f]{32}")
SPAN_ID = re.compile(r"[0-9a-f]{16}")

_host = trace.get_tracer("tests.host")


def _event() -> EventDict:
    return add_trace_ids(None, "info", {"event": "x"})


def test_the_api_re_exports_the_processor_by_identity() -> None:
    assert api.add_trace_ids is add_trace_ids
    assert "add_trace_ids" in api.__all__


def test_a_recording_span_puts_its_own_ids_on_the_record(
    otel_spans: InMemorySpanExporter,  # noqa: F811
) -> None:
    with _host.start_as_current_span("host") as span:
        assert span.is_recording()
        event = _event()
        context = span.get_span_context()
    assert TRACE_ID.fullmatch(str(event["trace_id"]))
    assert SPAN_ID.fullmatch(str(event["span_id"]))
    assert event["trace_id"] == f"{context.trace_id:032x}"
    assert event["span_id"] == f"{context.span_id:016x}"


def test_the_ids_are_flat_strings(otel_spans: InMemorySpanExporter) -> None:  # noqa: F811
    with _host.start_as_current_span("host"):
        event = _event()
    assert isinstance(event["trace_id"], str)
    assert isinstance(event["span_id"], str)
    assert "span" not in event
    assert set(event) == {"event", "trace_id", "span_id"}


def test_a_sampled_out_span_adds_no_key(otel_spans: InMemorySpanExporter) -> None:  # noqa: F811
    drop_everything()
    with _host.start_as_current_span("dropped") as span:
        assert span.get_span_context().is_valid
        assert not span.is_recording()
        event = _event()
    assert "trace_id" not in event
    assert "span_id" not in event


def test_outside_any_span_no_key_is_added(otel_spans: InMemorySpanExporter) -> None:  # noqa: F811
    assert not trace.get_current_span().is_recording()
    event = _event()
    assert event == {"event": "x"}


def test_the_processor_returns_the_same_dict(otel_spans: InMemorySpanExporter) -> None:  # noqa: F811
    original: EventDict = {"event": "x"}
    with _host.start_as_current_span("host"):
        assert add_trace_ids(None, "info", original) is original


@pytest.mark.parametrize("with_slot", [True, False], ids=["slot-filled", "default"])
def test_configure_logging_carries_ids_only_when_the_host_fills_the_slot(
    with_slot: bool,
    otel_spans: InMemorySpanExporter,  # noqa: F811
    capsys: pytest.CaptureFixture[str],
) -> None:
    if with_slot:
        configure_logging("INFO", "json", trace_processors=(add_trace_ids,))
    else:
        configure_logging("INFO", "json")
    with _host.start_as_current_span("host") as span:
        structlog.get_logger("t").info("inside")
        context = span.get_span_context()
    structlog.get_logger("t").info("outside")
    inside, outside = (
        json.loads(line) for line in capsys.readouterr().err.splitlines() if line
    )
    assert "trace_id" not in outside
    if with_slot:
        assert inside["trace_id"] == f"{context.trace_id:032x}"
        assert inside["span_id"] == f"{context.span_id:016x}"
    else:
        assert "trace_id" not in inside
        assert "span_id" not in inside
