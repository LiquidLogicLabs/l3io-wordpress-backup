"""AD-10 — one ``phase_completed`` record per backup step.

:func:`phase` times a step on the monotonic clock and, on exit, emits exactly
one ``phase_completed`` record carrying ``phase``, ``duration_ms``, ``bytes``
(set by the step through the yielded :class:`PhaseRecord`; ``0`` when not
applicable), ``outcome`` (``ok``/``failed``, or :data:`SKIPPED_OUTCOME` for a
step a no-op command adapter stood in for) and ``exit_code``, plus any
step-specific ``fields`` the step adds. An exception is recorded as ``failed``
and re-raised unchanged. :func:`join_phase` lets a step that may run inside
its own phase (verification, inside the store's ``VERIFY``) annotate that
record rather than emit a second one.

``exit_code`` is ``0`` on success and, on failure, the exception's
``exit_code`` (``domain.errors.exit_code_of`` — the same mapping the CLI's
``exit_code_for`` applies, which ``adapters/`` may not import), so the failing
phase's record and ``run_finished`` carry the same code (FR-21). Nothing
here configures logging; the Run ID reaches the record through
``structlog.contextvars`` (AD-10).

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

from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.domain.errors import exit_code_of
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.ports.tracing import Redactor, Tracer

__all__ = [
    "ACTIVE_TRACER",
    "SKIPPED_OUTCOME",
    "PhaseRecord",
    "join_open_phase",
    "join_phase",
    "phase",
]

_log = structlog.get_logger(__name__)

_NS_PER_MS = 1_000_000
_FIXED_KEYS: Final = frozenset(
    {"phase", "duration_ms", "bytes", "outcome", "exit_code"}
)

#: The outcome a no-op command adapter's step records: nothing was performed,
#: so a rehearsal's log never claims an effect occurred (triage E-8).
SKIPPED_OUTCOME: Final = "skipped_dry_run"

_current: ContextVar[PhaseRecord | None] = ContextVar("phase", default=None)

#: The open Run's tracer and registry; ``None`` means ``NoopTracer()``.
ACTIVE_TRACER: ContextVar[tuple[Tracer, Redactor] | None] = ContextVar(
    "active_tracer", default=None
)


def _tracer() -> tuple[Tracer, Redactor]:
    # Imported here: ``adapters.noop``'s command no-ops record through
    # ``phase``, so a module-level import would be circular.
    from l3io.wp.backup.adapters.noop import NoopTracer

    active = ACTIVE_TRACER.get()
    # Outside a Run nothing is registered: an empty registry redacts nothing.
    return active if active is not None else (NoopTracer(), SecretRegistry())


@dataclass(slots=True)
class PhaseRecord:
    """What a step reports about itself: ``bytes``, and any extra ``fields``.

    :meth:`fail` marks a step that returns a failure rather than raising (a
    verification that did not pass), so its record says ``failed`` with the
    same code the Run finishes with. :meth:`skip` marks a step a no-op
    command adapter stood in for: its record says :data:`SKIPPED_OUTCOME`
    with ``bytes=0``.
    """

    name: str
    bytes: int = 0
    fields: dict[str, object] = field(default_factory=dict)
    failure: ExitCode | None = None
    skipped: bool = False

    def fail(self, exit_code: ExitCode) -> None:
        """Record this step as failed with ``exit_code`` without raising."""
        self.failure = exit_code

    def skip(self) -> None:
        """Record this step as not performed; it moved no bytes."""
        self.skipped = True
        self.bytes = 0


@contextmanager
def phase(name: str) -> Iterator[PhaseRecord]:
    """Run the body as phase ``name`` and emit its ``phase_completed`` record."""
    tracer, secrets = _tracer()
    with tracer.phase_span(name, secrets=secrets) as span:
        record = PhaseRecord(name=name)
        token = _current.set(record)
        started = time.monotonic_ns()
        outcome, exit_code = "failed", ExitCode.FAILED
        try:
            yield record
            if record.failure is None:
                outcome = SKIPPED_OUTCOME if record.skipped else "ok"
                exit_code = ExitCode.OK
            else:
                exit_code = record.failure
        except BaseException as exc:
            exit_code = exit_code_of(exc)
            raise
        finally:
            _current.reset(token)
            _emit(record, started, outcome, exit_code)
            span.set_outcome(exit_code=int(exit_code), archive_bytes=record.bytes)


def _emit(record: PhaseRecord, started: int, outcome: str, exit_code: ExitCode) -> None:
    emit = _log.error if outcome == "failed" else _log.info
    extra = {k: v for k, v in record.fields.items() if k not in _FIXED_KEYS}
    emit(
        "phase_completed",
        phase=record.name,
        duration_ms=(time.monotonic_ns() - started) / _NS_PER_MS,
        bytes=record.bytes,
        outcome=outcome,
        exit_code=int(exit_code),
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


@contextmanager
def join_open_phase(fallback: str) -> Iterator[PhaseRecord]:
    """The innermost open phase, whatever its name; else a new ``fallback``.

    For the Dry Run's command no-ops: the executor decides which phase a call
    belongs to (AD-17), so a no-op called inside an open phase adds to that
    phase's record and never emits one of its own. ``fallback`` names the
    phase only when the no-op is called outside any (BL-E002-107).
    """
    current = _current.get()
    if current is not None:
        yield current
        return
    with phase(fallback) as record:
        yield record
