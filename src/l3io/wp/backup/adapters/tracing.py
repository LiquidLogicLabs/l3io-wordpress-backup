"""AD-24 — the OpenTelemetry tracer, inert unless a host configures an SDK.

:class:`OpenTelemetryTracer` implements ``ports.tracing.Tracer`` over the
OpenTelemetry **API** only. Nothing here configures a tracer provider, a
sampler or an exporter: with no SDK installed the global provider is the
API's proxy, every span is non-recording, and nothing is computed, emitted or
raised. A host that runs an SDK sees one ``l3io.wp.backup.run`` span per Run,
a child of its own current span, and one ``l3io.wp.backup.<phase>`` span per
``phase_completed`` record (``adapters.phase.phase`` opens it), each the child
of the span that encloses it. Database's own ``l3io.wp.database.*`` spans
nest under the phase span by context propagation; no span is opened per file
or per database call.

The tracer is resolved at call time, never cached at import, so a provider
the host installs after this package was imported is honoured.

**Attributes.** This package's own keys are ``l3io.*`` (AD-24 as amended
2026-11-06, owner decision O-2 (a)): the root's ``attributes`` mapping is set
verbatim, plus ``l3io.run_id``, ``l3io.phase`` and ``l3io.archive.bytes``.
Semantic-convention keys keep their names: ``db.name``, ``error.type``,
``process.exit.code``, ``process.executable.name`` and
``process.command_args``.

**Redaction (ADR-0018).** A span attribute is as public as a log line. Every
free-text value — ``db.name``, ``process.command_args``, a status
description — goes through the Run's registry (``secrets``, the one
implementation in ``adapters.redaction``); only enum values, booleans and
integers are set raw. No exception event is recorded (``record_exception``
is off), so none of *this package's* spans carries a stack trace, and the
status description of a non-package exception names its class only. The
database dependency's own ``l3io.wp.database.*`` spans nest under ours; from
database 0.3.3 (the floor, BL-E006-010) a failed one carries only
``error.type`` and an ERROR status with no description, and records no
exception event.
Attribute values are computed only while ``span.is_recording()``: a
non-recording span costs no redaction call.

**Failures.** ``cli.context.open_run`` wraps every tracer in
``adapters.noop.GuardedTracer``, which logs ``tracer_failed`` and disables
tracing for the Run when a tracer call raises; this module wraps nothing.

:func:`add_trace_ids` is the structlog processor for
``configure_logging(..., trace_processors=(add_trace_ids,))``: flat
``trace_id``/``span_id`` while a span is recording, and nothing otherwise.
structlog ships no such processor (its OpenTelemetry recipe is a
documentation example that nests the ids).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Final

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode
from structlog.typing import EventDict, WrappedLogger

from l3io.wp.backup import __version__
from l3io.wp.backup.domain.errors import BackupError
from l3io.wp.backup.ports.tracing import PhaseSpan, Redactor, RunSpan, SpanAttributes

__all__ = [
    "INSTRUMENTATION_NAME",
    "RUN_SPAN_NAME",
    "OpenTelemetryTracer",
    "add_trace_ids",
    "phase_span_name",
]

#: The instrumentation scope, and the prefix of every span name.
INSTRUMENTATION_NAME: Final = "l3io.wp.backup"
#: The root span of one Run.
RUN_SPAN_NAME: Final = f"{INSTRUMENTATION_NAME}.run"
#: ``process.executable.name`` when a root states a command line.
EXECUTABLE_NAME: Final = "wpbackup2"


def phase_span_name(phase: str) -> str:
    """``l3io.wp.backup.<phase lowercased>``."""
    return f"{INSTRUMENTATION_NAME}.{phase.lower()}"


def _tracer() -> trace.Tracer:
    return trace.get_tracer(INSTRUMENTATION_NAME, __version__)


@contextmanager
def _current_span(name: str) -> Iterator[Span]:
    with _tracer().start_as_current_span(
        name, record_exception=False, set_status_on_exception=False
    ) as span:
        yield span


def _fail(span: Span, exc: BaseException, secrets: Redactor) -> None:
    """Mark ``span`` failed; only a ``BackupError`` says why.

    A ``BackupError``'s message is built from its own fields. Any other
    exception is named by class and its text is withheld: a dependency's text
    is untrusted, the registry removes only the secrets it knows, and a span
    leaves the program for a backend outside the host's log controls. This is
    the rule ``cli.exit`` applies to the traceback it prints.
    """
    description = (
        secrets.redact(str(exc))
        if isinstance(exc, BackupError)
        else f"{type(exc).__name__} (message withheld: not a package error)"
    )
    span.set_status(Status(StatusCode.ERROR, description))


class _RunSpan:
    """``ports.tracing.RunSpan`` over one OpenTelemetry span."""

    __slots__ = ("_secrets", "_span")

    def __init__(self, span: Span, secrets: Redactor) -> None:
        self._span, self._secrets = span, secrets

    def __repr__(self) -> str:
        # The registry holds the Run's secrets: never part of a repr (AD-7).
        return f"_RunSpan(recording={self._span.is_recording()})"

    def set_attributes(
        self, *, db_name: str | None = None, archive_bytes: int | None = None
    ) -> None:
        if not self._span.is_recording():
            return
        if db_name is not None:
            self._span.set_attribute("db.name", self._secrets.redact(db_name))
        if archive_bytes is not None:
            self._span.set_attribute("l3io.archive.bytes", archive_bytes)

    def set_outcome(self, *, exit_code: int, error_type: str | None) -> None:
        if not self._span.is_recording():
            return
        self._span.set_attribute("process.exit.code", exit_code)
        if error_type is not None:
            self._span.set_attribute("error.type", error_type)
        if exit_code != 0:
            self._span.set_status(Status(StatusCode.ERROR))


class _PhaseSpan:
    """``ports.tracing.PhaseSpan`` over one OpenTelemetry span."""

    __slots__ = ("_span",)

    def __init__(self, span: Span) -> None:
        self._span = span

    def set_outcome(self, *, exit_code: int, archive_bytes: int | None) -> None:
        if not self._span.is_recording():
            return
        self._span.set_attribute("process.exit.code", exit_code)
        if archive_bytes is not None:
            self._span.set_attribute("l3io.archive.bytes", archive_bytes)
        if exit_code != 0:
            self._span.set_status(Status(StatusCode.ERROR))


class OpenTelemetryTracer:
    """``ports.tracing.Tracer`` over the OpenTelemetry API (AD-24)."""

    __slots__ = ()

    @contextmanager
    def run_span(
        self,
        *,
        run_id: str,
        operation: str,
        attributes: SpanAttributes,
        command_args: Sequence[str] | None,
        secrets: Redactor,
    ) -> Iterator[RunSpan]:
        """The current ``l3io.wp.backup.run`` span.

        ``operation`` is not set again: the root's ``attributes`` carry it.
        On an exception the span's status is ``ERROR`` with the redacted
        message; ``error.type`` is the root's, through ``set_outcome``.
        """
        del operation
        with _current_span(RUN_SPAN_NAME) as span:
            if span.is_recording():
                span.set_attributes(dict(attributes))
                span.set_attribute("l3io.run_id", run_id)
                if command_args is not None:
                    span.set_attribute("process.executable.name", EXECUTABLE_NAME)
                    span.set_attribute(
                        "process.command_args", secrets.redact_argv(command_args)
                    )
            try:
                yield _RunSpan(span, secrets)
            except BaseException as exc:
                if span.is_recording():
                    _fail(span, exc, secrets)
                raise

    @contextmanager
    def phase_span(self, phase: str, *, secrets: Redactor) -> Iterator[PhaseSpan]:
        """The current ``l3io.wp.backup.<phase>`` span, with ``l3io.phase``.

        On an exception the span's status is ``ERROR`` with the redacted
        message, and ``error.type`` is the exception's class name — the same
        spelling the root's ``set_outcome`` receives.
        """
        with _current_span(phase_span_name(phase)) as span:
            if span.is_recording():
                span.set_attribute("l3io.phase", phase)
            try:
                yield _PhaseSpan(span)
            except BaseException as exc:
                if span.is_recording():
                    span.set_attribute("error.type", type(exc).__name__)
                    _fail(span, exc, secrets)
                raise


def add_trace_ids(
    logger: WrappedLogger, method_name: str, event_dict: EventDict
) -> EventDict:
    """Flat ``trace_id`` (32 hex) and ``span_id`` (16 hex) while a span records.

    A non-recording span — no SDK, or a sampler that dropped the Run — adds
    no key at all.
    """
    del logger, method_name
    span = trace.get_current_span()
    if span.is_recording():
        context = span.get_span_context()
        event_dict["trace_id"] = trace.format_trace_id(context.trace_id)
        event_dict["span_id"] = trace.format_span_id(context.span_id)
    return event_dict
