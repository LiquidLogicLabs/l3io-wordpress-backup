"""One in-memory OpenTelemetry SDK for the tracing tests (E006-S01-004).

Not a test module. OpenTelemetry lets the global tracer provider be set once
per process, so :func:`install` sets it once — an SDK ``TracerProvider``
exporting through a ``SimpleSpanProcessor`` into the SDK's own
``InMemorySpanExporter`` — and every later call returns the same exporter.
The provider's sampler is :data:`SAMPLER`, which delegates to ``ALWAYS_ON``
unless a test switches it to ``ALWAYS_OFF`` (the sampler-dropped Run). The
no-SDK cases run in a fresh interpreter instead (``tests/contracts``).

The fixture :func:`otel_spans` installs the SDK, clears the exporter and
restores ``ALWAYS_ON`` around each test; import it into a test module to
register it.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any

import pytest

pytest.importorskip("opentelemetry.sdk")

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.sdk.trace.sampling import (
    ALWAYS_OFF,
    ALWAYS_ON,
    Sampler,
    SamplingResult,
)
from opentelemetry.trace import Link, SpanKind
from opentelemetry.util.types import Attributes


class SwitchableSampler(Sampler):
    """A sampler that delegates to :attr:`delegate`, switched per test."""

    def __init__(self) -> None:
        self.delegate: Sampler = ALWAYS_ON

    def should_sample(
        self,
        parent_context: Context | None,
        trace_id: int,
        name: str,
        kind: SpanKind | None = None,
        attributes: Attributes = None,
        links: Sequence[Link] | None = None,
        trace_state: Any = None,  # noqa: ANN401 - the SDK's signature
    ) -> SamplingResult:
        return self.delegate.should_sample(
            parent_context, trace_id, name, kind, attributes, links, trace_state
        )

    def get_description(self) -> str:
        return f"Switchable({self.delegate.get_description()})"


SAMPLER = SwitchableSampler()
_EXPORTER: list[InMemorySpanExporter] = []


def install() -> InMemorySpanExporter:
    """The process's one SDK exporter, installing the provider on first call."""
    if not _EXPORTER:
        exporter = InMemorySpanExporter()
        provider = TracerProvider(sampler=SAMPLER)
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        if trace.get_tracer_provider() is not provider:
            msg = "another tracer provider was installed first"
            raise RuntimeError(msg)
        _EXPORTER.append(exporter)
    return _EXPORTER[0]


def drop_everything() -> None:
    """Make every new root span non-recording (an ``ALWAYS_OFF`` sampler)."""
    SAMPLER.delegate = ALWAYS_OFF


@pytest.fixture
def otel_spans() -> Iterator[InMemorySpanExporter]:
    """The in-memory exporter, empty, with spans sampled."""
    exporter = install()
    exporter.clear()
    SAMPLER.delegate = ALWAYS_ON
    yield exporter
    SAMPLER.delegate = ALWAYS_ON
    exporter.clear()


def by_name(spans: Sequence[ReadableSpan], prefix: str) -> list[ReadableSpan]:
    """The spans whose name starts with ``prefix``, in start order."""
    return sorted(
        (s for s in spans if s.name.startswith(prefix)),
        key=lambda s: s.start_time or 0,
    )


def parent_id(span: ReadableSpan) -> int | None:
    """The span id of ``span``'s parent, or ``None`` for a trace root."""
    return None if span.parent is None else span.parent.span_id


def span_id(span: ReadableSpan) -> int:
    context = span.get_span_context()
    assert context is not None
    return context.span_id


__all__ = [
    "SAMPLER",
    "SwitchableSampler",
    "by_name",
    "drop_everything",
    "install",
    "otel_spans",
    "parent_id",
    "span_id",
]
