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
and ``1`` on failure. E002-S01-004 adds the tracing hook and binds the Run ID.
Nothing here configures logging.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Final

import structlog

__all__ = ["PhaseRecord", "join_phase", "phase"]

_log = structlog.get_logger(__name__)

_NS_PER_MS = 1_000_000
_FIXED_KEYS: Final = frozenset(
    {"phase", "duration_ms", "bytes", "outcome", "exit_code"}
)

_current: ContextVar[PhaseRecord | None] = ContextVar("phase", default=None)


@dataclass(slots=True)
class PhaseRecord:
    """What a step reports about itself: ``bytes``, and any extra ``fields``."""

    name: str
    bytes: int = 0
    fields: dict[str, object] = field(default_factory=dict)


@contextmanager
def phase(name: str) -> Iterator[PhaseRecord]:
    """Run the body as phase ``name`` and emit its ``phase_completed`` record."""
    record = PhaseRecord(name=name)
    token = _current.set(record)
    started = time.monotonic_ns()
    outcome, exit_code = "failed", 1
    try:
        yield record
        outcome, exit_code = "ok", 0
    finally:
        _current.reset(token)
        emit = _log.info if outcome == "ok" else _log.error
        extra = {k: v for k, v in record.fields.items() if k not in _FIXED_KEYS}
        emit(
            "phase_completed",
            phase=name,
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
