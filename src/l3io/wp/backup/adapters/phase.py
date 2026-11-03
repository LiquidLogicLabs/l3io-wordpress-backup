"""AD-10 — one ``phase_completed`` record per backup step.

:func:`phase` times a step on the monotonic clock and, on exit, emits exactly
one ``phase_completed`` record carrying ``phase``, ``duration_ms``, ``bytes``
(set by the step through the yielded :class:`PhaseRecord`; ``0`` when not
applicable), ``outcome`` (``ok``/``failed``) and ``exit_code``, plus any
step-specific ``fields`` the step adds. An exception is recorded as ``failed``
and re-raised unchanged. :func:`join_phase` lets a step that may run inside
its own phase (verification, inside the store's ``VERIFY``) annotate that
record rather than emit a second one.

Until E002-S01-005 maps errors to exit codes, ``exit_code`` is ``0`` on success
and ``1`` on failure. Nothing here configures logging; the Run ID reaches the
record through ``structlog.contextvars`` (AD-10).

**Tracing hook (AD-24).** :data:`ACTIVE_TRACER` holds the open Run's tracer and
registry, set and reset by ``cli.context.open_run``; ``None`` means
``NoopTracer()``. :func:`phase` enters ``tracer.phase_span(name, secrets=...)``
around the block and calls its ``set_outcome`` with the ``exit_code`` and
``bytes`` it logs, so ``phase()`` needs no Run Context.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Final

import structlog

from l3io.wp.backup.adapters.noop import NoopTracer
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.ports.tracing import Redactor, Tracer

__all__ = ["ACTIVE_TRACER", "PhaseRecord", "join_phase", "phase"]

_log = structlog.get_logger(__name__)

_NS_PER_MS = 1_000_000
_FIXED_KEYS: Final = frozenset(
    {"phase", "duration_ms", "bytes", "outcome", "exit_code"}
)

_current: ContextVar[PhaseRecord | None] = ContextVar("phase", default=None)

#: The open Run's tracer and registry; ``None`` means ``NoopTracer()``.
ACTIVE_TRACER: ContextVar[tuple[Tracer, Redactor] | None] = ContextVar(
    "active_tracer", default=None
)


def _tracer() -> tuple[Tracer, Redactor]:
    active = ACTIVE_TRACER.get()
    # Outside a Run nothing is registered: an empty registry redacts nothing.
    return active if active is not None else (NoopTracer(), SecretRegistry())


@dataclass(slots=True)
class PhaseRecord:
    """What a step reports about itself: ``bytes``, and any extra ``fields``."""

    name: str
    bytes: int = 0
    fields: dict[str, object] = field(default_factory=dict)


@contextmanager
def phase(name: str) -> Iterator[PhaseRecord]:
    """Run the body as phase ``name`` and emit its ``phase_completed`` record."""
    tracer, secrets = _tracer()
    with tracer.phase_span(name, secrets=secrets) as span:
        record = PhaseRecord(name=name)
        token = _current.set(record)
        started = time.monotonic_ns()
        outcome, exit_code = "failed", 1
        try:
            yield record
            outcome, exit_code = "ok", 0
        finally:
            _current.reset(token)
            _emit(record, started, outcome, exit_code)
            span.set_outcome(exit_code=exit_code, archive_bytes=record.bytes)


def _emit(record: PhaseRecord, started: int, outcome: str, exit_code: int) -> None:
    emit = _log.info if outcome == "ok" else _log.error
    extra = {k: v for k, v in record.fields.items() if k not in _FIXED_KEYS}
    emit(
        "phase_completed",
        phase=record.name,
        duration_ms=(time.monotonic_ns() - started) / _NS_PER_MS,
        bytes=record.bytes,
        outcome=outcome,
        exit_code=exit_code,
        **extra,
    )


@contextmanager
def join_phase(name: str) -> Iterator[PhaseRecord]:
    """The innermost open phase when it is ``name``; otherwise a new one."""
    current = _current.get()
    if current is not None and current.name == name:
        yield current
        return
    with phase(name) as record:
        yield record
