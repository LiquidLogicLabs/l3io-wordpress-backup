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

**A phase is joined by identity, not by its logged name.** Backup's
``BackupStep.VERIFY`` and restore's ``RestorePhase.VERIFY`` are both logged
``VERIFY`` (BL-E002-109), so a name cannot tell them apart. :func:`phase` takes
a :data:`PhaseKey` (the step's enum member, or a plain string for a phase with
no enum) and :func:`join_phase` joins the innermost open phase only when its
key is the same key: same type and same value. A restore's ``VERIFY`` and the
Safety Snapshot backup's ``VERIFY`` never join each other.

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
from enum import Enum
from typing import Final, TypeAlias

import structlog

from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.core.backup import BackupStep
from l3io.wp.backup.domain.errors import exit_code_of
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.domain.restore import RestorePhase
from l3io.wp.backup.ports.tracing import Redactor, Tracer

__all__ = [
    "ACTIVE_TRACER",
    "SKIPPED_OUTCOME",
    "PhaseKey",
    "PhaseRecord",
    "join_open_phase",
    "join_phase",
    "phase",
    "phase_name",
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


#: What identifies a phase: a backup step, a restore phase, or a plain name
#: for a phase with no enum (the Run Journal's ``JOURNAL``).
PhaseKey: TypeAlias = BackupStep | RestorePhase | str


def phase_name(key: PhaseKey) -> str:
    """The name ``key``'s record and span carry (upper case, BL-E002-109)."""
    if isinstance(key, RestorePhase):
        return key.name
    if isinstance(key, Enum):
        return str(key.value)
    return key


def _same_phase(a: PhaseKey, b: PhaseKey) -> bool:
    """Whether two keys are one phase: same type and value, never only a name."""
    return type(a) is type(b) and a == b


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
    key: PhaseKey = ""
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
def phase(key: PhaseKey) -> Iterator[PhaseRecord]:
    """Run the body as phase ``key`` and emit its ``phase_completed`` record."""
    name = phase_name(key)
    tracer, secrets = _tracer()
    with tracer.phase_span(name, secrets=secrets) as span:
        record = PhaseRecord(name=name, key=key)
        token = _current.set(record)
        started = time.monotonic_ns()
        outcome = "failed"
        exit_code: ExitCode | None = ExitCode.FAILED
        try:
            yield record
            if record.failure is None:
                outcome = SKIPPED_OUTCOME if record.skipped else "ok"
                exit_code = ExitCode.OK
            else:
                exit_code = record.failure
        except BaseException as exc:
            # Not an Exception (KeyboardInterrupt): the process exits with the
            # interpreter's status, not this table's, so no code is claimed
            # (BL-E002-056).
            exit_code = exit_code_of(exc) if isinstance(exc, Exception) else None
            raise
        finally:
            _current.reset(token)
            _emit(record, started, outcome, exit_code)
            if exit_code is not None:
                span.set_outcome(exit_code=int(exit_code), archive_bytes=record.bytes)


def _emit(
    record: PhaseRecord, started: int, outcome: str, exit_code: ExitCode | None
) -> None:
    emit = _log.error if outcome == "failed" else _log.info
    extra = {k: v for k, v in record.fields.items() if k not in _FIXED_KEYS}
    emit(
        "phase_completed",
        phase=record.name,
        duration_ms=(time.monotonic_ns() - started) / _NS_PER_MS,
        bytes=record.bytes,
        outcome=outcome,
        exit_code=None if exit_code is None else int(exit_code),
        **extra,
    )


@contextmanager
def join_phase(key: PhaseKey) -> Iterator[PhaseRecord]:
    """The innermost open phase when it is the phase ``key``; otherwise a new one.

    Matched by key (type and value), never by the logged name: a restore's
    ``RestorePhase.VERIFY`` is not a backup's ``BackupStep.VERIFY``.
    """
    current = _current.get()
    if current is not None and _same_phase(current.key, key):
        yield current
        return
    with phase(key) as record:
        yield record


@contextmanager
def join_open_phase(fallback: PhaseKey) -> Iterator[PhaseRecord]:
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
