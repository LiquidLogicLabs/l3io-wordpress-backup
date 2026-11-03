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

``dry_run`` is the root's Dry Run selection (AD-4): bound once as a context
variable, so every record in the Run carries it (``run_started`` included),
and stated on the root span as ``l3io.dry_run``, and kept on the context as
:attr:`RunContext.dry_run` so a joining driver can see it. It selects nothing
here; the driver selects the adapter set
(``cli.adapter_sets.dry_run_adapters``). A joined Run inherits the outer root's
selection, with one refusal: a Dry Run request cannot join a real Run —
:func:`join_or_open` raises ``UsageError`` rather than let a rehearsal write.
A real request joining a Dry Run root stays safe and inherits it.

:func:`join_or_open` is what a driver calls: given a context it joins it and
creates nothing; otherwise it opens one. A driver binds adapters only when the
Run was opened here (``RunHandle.opened``), so a set is bound exactly once.

``run_finished`` carries ``exit_code`` as the integer and ``exit_reason`` as
the :class:`~l3io.wp.backup.domain.exit_codes.ExitCode` member name (FR-21):
``OK`` on success, the code a driver reported (:meth:`RunContext.report_failure`),
or :func:`~l3io.wp.backup.cli.exit.exit_code_for` of an escaping exception.
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
from l3io.wp.backup.cli.exit import RUN_ID_ATTR, exit_code_for
from l3io.wp.backup.domain.errors import (
    AdaptersAlreadyBoundError,
    AdaptersNotBoundError,
    RunContextActiveError,
    UsageError,
)
from l3io.wp.backup.domain.exit_codes import ExitCode
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
        self.outcome: tuple[ExitCode, str | None] | None = None


@dataclass(frozen=True, slots=True)
class RunContext:
    """One Run: its identity, logger, clock, registry, tracer and workspace.

    ``dry_run`` is the selection the Run's root made (AD-4), as data.
    """

    run_id: str
    started_at: datetime
    workspace: Path
    logger: structlog.stdlib.BoundLogger
    clock: Clock
    secrets: SecretRegistry
    tracer: Tracer
    run_span: RunSpan
    dry_run: bool = False
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

    def report_failure(self, *, exit_code: ExitCode, error_type: str) -> None:
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


def _outcome(state: _RunState) -> tuple[ExitCode, str | None]:
    """A Run with no exception: ``OK``, or the failure its driver reported."""
    return (ExitCode.OK, None) if state.outcome is None else state.outcome


@contextmanager
def open_run(
    *,
    operation: str,
    tracer: Tracer | None = None,
    secrets: SecretRegistry | None = None,
    command_args: Sequence[str] | None = None,
    workspace_parent: Path | None = None,
    dry_run: bool = False,
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
        tokens = structlog.contextvars.bind_contextvars(run_id=run_id, dry_run=dry_run)
        stack.callback(structlog.contextvars.reset_contextvars, **tokens)
        stack.enter_context(open_registry(registry))
        tracer_token = ACTIVE_TRACER.set((guarded, registry))
        stack.callback(ACTIVE_TRACER.reset, tracer_token)
        run_span = stack.enter_context(
            guarded.run_span(
                run_id=run_id,
                operation=operation,
                # The root states the Dry Run fact (AD-4).
                attributes={"l3io.operation": operation, "l3io.dry_run": dry_run},
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
            dry_run=dry_run,
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
                    dry_run=dry_run,
                )
                ctx_token = _ACTIVE.set(ctx)
                try:
                    yield ctx
                finally:
                    _ACTIVE.reset(ctx_token)
        except BaseException as exc:
            error = exc
            _stamp_run_id(exc, run_id)
            raise
        finally:
            exit_code, error_type = (
                (exit_code_for(error), type(error).__name__)
                if error is not None
                else _outcome(run_state)
            )
            registry.log_registered()
            emit = _log.info if exit_code is ExitCode.OK else _log.error
            emit(
                "run_finished",
                run_id=run_id,
                exit_code=int(exit_code),
                exit_reason=exit_code.name,
                duration_ms=(time.monotonic_ns() - started) / _NS_PER_MS,
                error=error_type,
            )
            run_span.set_outcome(exit_code=int(exit_code), error_type=error_type)


def _stamp_run_id(exc: BaseException, run_id: str) -> None:
    """Leave the failed Run's id on ``exc`` so the entry point can log it.

    ``command_failed`` is written after the Run closed and its ``run_id`` was
    unbound; the id travels on the exception instead. An exception that cannot
    take an attribute simply keeps the old behaviour (no ``run_id``).
    """
    try:
        if not hasattr(exc, RUN_ID_ATTR):
            setattr(exc, RUN_ID_ATTR, run_id)
    except (AttributeError, TypeError):
        pass


@contextmanager
def join_or_open(
    ctx: RunContext | None,
    *,
    operation: str,
    tracer: Tracer | None = None,
    secrets: SecretRegistry | None = None,
    command_args: Sequence[str] | None = None,
    dry_run: bool = False,
) -> Iterator[RunHandle]:
    """Join ``ctx`` — creating, binding and opening nothing — or open a Run.

    ``dry_run`` applies only to a Run opened here; a joined Run keeps the
    selection its own root made. Joining a real Run with ``dry_run=True``
    raises :class:`UsageError`: the request asked for a rehearsal and the Run
    would write.
    """
    if ctx is not None:
        if dry_run and not ctx.dry_run:
            msg = (
                f"a Dry Run {operation} cannot join Run {ctx.run_id}, which is "
                "not a Dry Run"
            )
            raise UsageError(msg)
        yield RunHandle(ctx=ctx, opened=False)
        return
    with open_run(
        operation=operation,
        tracer=tracer,
        secrets=secrets,
        command_args=command_args,
        dry_run=dry_run,
    ) as opened:
        yield RunHandle(ctx=opened, opened=True)
