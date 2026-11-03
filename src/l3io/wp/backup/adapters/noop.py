"""AD-24 — the inert tracer, and the guard that falls back to it.

:class:`NoopTracer` implements ``ports.tracing.Tracer``, ``RunSpan`` and
``PhaseSpan`` and records nothing; ``tracer=None`` means ``NoopTracer()``.

:class:`GuardedTracer` wraps the Run's tracer so tracing never changes an
outcome: the first exception any tracer call raises is logged once, at
WARNING, as ``tracer_failed`` with the tracer's type name, and every later
call for that Run is a no-op. Only ``Exception`` is caught —
``KeyboardInterrupt`` and ``SystemExit`` still propagate.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from typing import Final, TypeVar

import structlog

from l3io.wp.backup.ports.tracing import (
    PhaseSpan,
    Redactor,
    RunSpan,
    SpanAttributes,
    Tracer,
)

__all__ = ["GuardedTracer", "NoopTracer"]

_log = structlog.get_logger(__name__)

_T = TypeVar("_T")


class NoopTracer:
    """A ``Tracer`` that is also its own ``RunSpan`` and ``PhaseSpan``."""

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
        """Nothing is recorded."""
        del run_id, operation, attributes, command_args, secrets
        yield self

    @contextmanager
    def phase_span(self, phase: str, *, secrets: Redactor) -> Iterator[PhaseSpan]:
        """Nothing is recorded."""
        del phase, secrets
        yield self

    def set_attributes(
        self, *, db_name: str | None = None, archive_bytes: int | None = None
    ) -> None:
        """Nothing is recorded."""
        del db_name, archive_bytes

    def set_outcome(
        self,
        *,
        exit_code: int,
        error_type: str | None = None,
        archive_bytes: int | None = None,
    ) -> None:
        """Nothing is recorded."""
        del exit_code, error_type, archive_bytes


_NOOP: Final = NoopTracer()
_NOOP_RUN_SPAN: Final[RunSpan] = _NOOP
_NOOP_PHASE_SPAN: Final[PhaseSpan] = _NOOP


class _Failed:
    """The result of a tracer call that raised."""


_FAILED: Final = _Failed()


class GuardedTracer:
    """A ``Tracer`` whose calls never raise; the first failure disables it."""

    __slots__ = ("_disabled", "_inner")

    def __init__(self, inner: Tracer) -> None:
        self._inner = inner
        self._disabled = False

    def __repr__(self) -> str:
        return f"GuardedTracer({type(self._inner).__name__})"

    @property
    def inner(self) -> Tracer:
        """The tracer being guarded."""
        return self._inner

    def call(self, fn: Callable[[], _T], *, closing: bool = False) -> _T | _Failed:
        """``fn()``, or :data:`_FAILED` once any call has raised.

        ``closing`` marks the exit of a span that was entered: it is attempted
        even after a failure, so a real tracer's span is still ended and its
        context detached; it never logs a second ``tracer_failed``.
        """
        if self._disabled and not closing:
            return _FAILED
        try:
            return fn()
        except Exception as exc:
            if not self._disabled:
                self._disabled = True
                _log.warning(
                    "tracer_failed",
                    tracer=type(self._inner).__name__,
                    error=type(exc).__name__,
                )
            return _FAILED

    @contextmanager
    def _span(
        self,
        opener: Callable[[], AbstractContextManager[_T]],
        wrap: Callable[[_T], _T],
        fallback: _T,
    ) -> Iterator[_T]:
        manager = self.call(opener)
        span: _T | _Failed = _FAILED
        if not isinstance(manager, _Failed):
            span = self.call(manager.__enter__)
        if isinstance(manager, _Failed) or isinstance(span, _Failed):
            yield fallback
            return
        try:
            yield wrap(span)
        except BaseException as exc:
            failure = exc
            self.call(
                lambda: manager.__exit__(type(failure), failure, failure.__traceback__),
                closing=True,
            )
            raise
        self.call(lambda: manager.__exit__(None, None, None), closing=True)

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
        """The inner tracer's root span, guarded; a no-op span on failure."""
        with self._span(
            lambda: self._inner.run_span(
                run_id=run_id,
                operation=operation,
                attributes=attributes,
                command_args=command_args,
                secrets=secrets,
            ),
            lambda span: _GuardedRunSpan(self, span),
            _NOOP_RUN_SPAN,
        ) as span:
            yield span

    @contextmanager
    def phase_span(self, phase: str, *, secrets: Redactor) -> Iterator[PhaseSpan]:
        """The inner tracer's phase span, guarded; a no-op span on failure."""
        with self._span(
            lambda: self._inner.phase_span(phase, secrets=secrets),
            lambda span: _GuardedPhaseSpan(self, span),
            _NOOP_PHASE_SPAN,
        ) as span:
            yield span


class _GuardedRunSpan:
    __slots__ = ("_guard", "_span")

    def __init__(self, guard: GuardedTracer, span: RunSpan) -> None:
        self._guard, self._span = guard, span

    def set_attributes(
        self, *, db_name: str | None = None, archive_bytes: int | None = None
    ) -> None:
        self._guard.call(
            lambda: self._span.set_attributes(
                db_name=db_name, archive_bytes=archive_bytes
            )
        )

    def set_outcome(self, *, exit_code: int, error_type: str | None) -> None:
        self._guard.call(
            lambda: self._span.set_outcome(exit_code=exit_code, error_type=error_type)
        )


class _GuardedPhaseSpan:
    __slots__ = ("_guard", "_span")

    def __init__(self, guard: GuardedTracer, span: PhaseSpan) -> None:
        self._guard, self._span = guard, span

    def set_outcome(self, *, exit_code: int, archive_bytes: int | None) -> None:
        self._guard.call(
            lambda: self._span.set_outcome(
                exit_code=exit_code, archive_bytes=archive_bytes
            )
        )
