"""AD-16/ADR-0016 — the Run Context: created once per Run, joined, never duplicated.

:func:`open_run` creates a :class:`RunContext` **without adapters**: a random
``run_id`` (UUIDv4, so it leaks neither time nor path), bound into
``structlog.contextvars`` so every record carries it (AD-10); the Run's
``SecretRegistry`` — the one the composition root created and resolved the
passphrase into, adopted as given, or a fresh one when given ``None``; the
root span; and the private workspace (AD-12). It logs ``run_started`` and, in
its ``finally``, ``run_finished`` with the exit code and duration, then removes
what it created and unbinds only what it bound — a caller's own
``correlation_id`` is never cleared.

:func:`join_or_open` is what a driver calls: given a context it joins it and
creates nothing; otherwise it opens one. A driver binds adapters only when the
Run was opened here (``RunHandle.opened``), so a set is bound exactly once.

The exit code is ``0`` on success and ``1`` on any exception until
E002-S01-005 maps errors to the exit-code table.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import structlog

from l3io.wp.backup import __version__
from l3io.wp.backup.adapters.clock import SystemClock
from l3io.wp.backup.adapters.noop import GuardedTracer, NoopTracer
from l3io.wp.backup.adapters.phase import ACTIVE_TRACER
from l3io.wp.backup.adapters.redaction import SecretRegistry, open_registry
from l3io.wp.backup.adapters.workspace import run_workspace
from l3io.wp.backup.domain.errors import (
    AdaptersAlreadyBoundError,
    AdaptersNotBoundError,
    RunContextActiveError,
)
from l3io.wp.backup.ports.adapter_set import AdapterSet
from l3io.wp.backup.ports.clock import Clock
from l3io.wp.backup.ports.tracing import RunSpan, Tracer

__all__ = [
    "RunContext",
    "RunHandle",
    "active_run",
    "join_or_open",
    "open_run",
]

_LOGGER_NAME = "l3io.wp.backup.run"
_NS_PER_MS = 1_000_000

_log = structlog.get_logger(_LOGGER_NAME)

_ACTIVE: ContextVar[RunContext | None] = ContextVar("active_run", default=None)


class _RunState:
    """The mutable part of a frozen Run: its adapter set and a reported outcome."""

    __slots__ = ("adapters", "outcome")

    def __init__(self) -> None:
        self.adapters: AdapterSet | None = None
        self.outcome: tuple[int, str | None] | None = None


@dataclass(frozen=True, slots=True)
class RunContext:
    """One Run: its identity, logger, clock, registry, tracer and workspace."""

    run_id: str
    started_at: datetime
    workspace: Path
    logger: structlog.stdlib.BoundLogger
    clock: Clock
    secrets: SecretRegistry
    tracer: Tracer
    run_span: RunSpan
    _state: _RunState = field(default_factory=_RunState, repr=False)

    def __repr__(self) -> str:
        # Neither the registry nor the workspace path (logged at DEBUG only).
        return f"RunContext(run_id={self.run_id!r})"

    @property
    def adapters(self) -> AdapterSet:
        """The bound set; :class:`AdaptersNotBoundError` before a bind."""
        if self._state.adapters is None:
            raise AdaptersNotBoundError(self.run_id)
        return self._state.adapters

    def bind_adapters(self, adapter_set: AdapterSet) -> None:
        """Bind the Run's adapters, exactly once."""
        if self._state.adapters is not None:
            raise AdaptersAlreadyBoundError(self.run_id)
        self._state.adapters = adapter_set

    def report_failure(self, *, exit_code: int, error_type: str) -> None:
        """A failure the driver returns rather than raises (a failed verify).

        ``run_finished`` and the root span then carry it; an exception escaping
        the Run still takes precedence.
        """
        self._state.outcome = (exit_code, error_type)


@dataclass(frozen=True, slots=True)
class RunHandle:
    """A Run a driver entered, and whether the driver opened it."""

    ctx: RunContext
    opened: bool


def active_run() -> RunContext | None:
    """The Run open in this context, if any."""
    return _ACTIVE.get()


def _outcome(state: _RunState) -> tuple[int, str | None]:
    """A Run with no exception: ``0``, or the failure its driver reported."""
    return (0, None) if state.outcome is None else state.outcome


@contextmanager
def open_run(
    *,
    operation: str,
    tracer: Tracer | None = None,
    secrets: SecretRegistry | None = None,
    command_args: Sequence[str] | None = None,
    workspace_parent: Path | None = None,
) -> Iterator[RunContext]:
    """Open a Run; :class:`RunContextActiveError` if one is already active."""
    active = _ACTIVE.get()
    if active is not None:
        raise RunContextActiveError(active.run_id)
    run_id = str(uuid.uuid4())
    registry = SecretRegistry() if secrets is None else secrets
    bound_tracer: Tracer = NoopTracer() if tracer is None else tracer
    guarded = GuardedTracer(bound_tracer)
    clock = SystemClock()
    started_at = clock.now()
    started = time.monotonic_ns()
    error: BaseException | None = None
    run_state = _RunState()
    with ExitStack() as stack:
        tokens = structlog.contextvars.bind_contextvars(run_id=run_id)
        stack.callback(structlog.contextvars.reset_contextvars, **tokens)
        stack.enter_context(open_registry(registry))
        tracer_token = ACTIVE_TRACER.set((guarded, registry))
        stack.callback(ACTIVE_TRACER.reset, tracer_token)
        run_span = stack.enter_context(
            guarded.run_span(
                run_id=run_id,
                operation=operation,
                # The root states the Dry Run fact (AD-4); E002-S02-007 sets it.
                attributes={"l3io.operation": operation, "l3io.dry_run": False},
                command_args=command_args,
                secrets=registry,
            )
        )
        _log.info(
            "run_started",
            run_id=run_id,
            started_at=started_at.isoformat().replace("+00:00", "Z"),
            operation=operation,
            version=__version__,
        )
        try:
            with run_workspace(workspace_parent) as workspace:
                ctx = RunContext(
                    _state=run_state,
                    run_id=run_id,
                    started_at=started_at,
                    workspace=workspace,
                    logger=structlog.stdlib.get_logger(_LOGGER_NAME),
                    clock=clock,
                    secrets=registry,
                    tracer=bound_tracer,
                    run_span=run_span,
                )
                ctx_token = _ACTIVE.set(ctx)
                try:
                    yield ctx
                finally:
                    _ACTIVE.reset(ctx_token)
        except BaseException as exc:
            error = exc
            raise
        finally:
            exit_code, error_type = (
                (1, type(error).__name__) if error is not None else _outcome(run_state)
            )
            registry.log_registered()
            emit = _log.info if exit_code == 0 else _log.error
            emit(
                "run_finished",
                run_id=run_id,
                exit_code=exit_code,
                duration_ms=(time.monotonic_ns() - started) / _NS_PER_MS,
                error=error_type,
            )
            run_span.set_outcome(exit_code=exit_code, error_type=error_type)


@contextmanager
def join_or_open(
    ctx: RunContext | None,
    *,
    operation: str,
    tracer: Tracer | None = None,
    secrets: SecretRegistry | None = None,
    command_args: Sequence[str] | None = None,
) -> Iterator[RunHandle]:
    """Join ``ctx`` — creating, binding and opening nothing — or open a Run."""
    if ctx is not None:
        yield RunHandle(ctx=ctx, opened=False)
        return
    with open_run(
        operation=operation,
        tracer=tracer,
        secrets=secrets,
        command_args=command_args,
    ) as opened:
        yield RunHandle(ctx=opened, opened=True)
