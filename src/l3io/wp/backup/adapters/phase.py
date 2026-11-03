"""AD-10 — one ``phase_completed`` record per backup step.

:func:`phase` times a step on the monotonic clock and, on exit, emits exactly
one ``phase_completed`` record carrying ``phase``, ``duration_ms``, ``bytes``
(set by the step through the yielded :class:`PhaseRecord`; ``0`` when not
applicable), ``outcome`` (``ok``/``failed``) and ``exit_code``. An exception
is recorded as ``failed`` and re-raised unchanged.

Until E002-S01-005 maps errors to exit codes, ``exit_code`` is ``0`` on success
and ``1`` on failure. E002-S01-004 adds the tracing hook and binds the Run ID.
Nothing here configures logging.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import structlog

__all__ = ["PhaseRecord", "phase"]

_log = structlog.get_logger(__name__)

_NS_PER_MS = 1_000_000


@dataclass(slots=True)
class PhaseRecord:
    """What a step reports about itself; ``bytes`` is the only field it sets."""

    name: str
    bytes: int = 0


@contextmanager
def phase(name: str) -> Iterator[PhaseRecord]:
    """Run the body as phase ``name`` and emit its ``phase_completed`` record."""
    record = PhaseRecord(name=name)
    started = time.monotonic_ns()
    outcome, exit_code = "failed", 1
    try:
        yield record
        outcome, exit_code = "ok", 0
    finally:
        emit = _log.info if outcome == "ok" else _log.error
        emit(
            "phase_completed",
            phase=name,
            duration_ms=(time.monotonic_ns() - started) / _NS_PER_MS,
            bytes=record.bytes,
            outcome=outcome,
            exit_code=exit_code,
        )
