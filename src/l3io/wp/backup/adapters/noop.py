"""AD-4/AD-24 — the no-op command adapters, the inert tracer and its guard.

**No-op command adapters (AD-4).** One per ``*Command`` Protocol under
``ports/``: :class:`NoopDatabase` (``DatabaseCommand``),
:class:`NoopArchiveWriter` (``ArchiveWriterCommand``) and
:class:`NoopArchiveStore` (``ArchiveStoreCommand``). A Dry Run binds them in
place of the real command halves (``cli.adapter_sets.dry_run_adapters``); the
query halves stay real. Each performs no effect, records the call into the
:class:`ActionLog` it was built with, emits ``phase_completed`` with
``outcome="skipped_dry_run"`` and ``bytes=0`` (joining the step's phase when the
executor has one open) and returns a value of its declared type that lets the
plan continue. The no-op writer reads no Site Root file and calls no exclusion
policy (ADR-0019); the no-op store's staged Archive is an ``os.devnull`` writer
whose ``publish()`` returns ``False`` and calls no verifier (ADR-0022).

:class:`NoopTracer` implements ``ports.tracing.Tracer``, ``RunSpan`` and
``PhaseSpan`` and records nothing; ``tracer=None`` means ``NoopTracer()``.

:class:`GuardedTracer` wraps the Run's tracer so tracing never changes an
outcome: the first exception any tracer call raises is logged once, at
WARNING, as ``tracer_failed`` with the tracer's type name, and every later
call for that Run is a no-op. Only ``Exception`` is caught —
``KeyboardInterrupt`` and ``SystemExit`` still propagate.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Final, TypeVar

import structlog

from l3io.wp.backup.adapters.phase import join_phase, phase
from l3io.wp.backup.core.backup import BackupStep
from l3io.wp.backup.domain.archive_format import Manifest
from l3io.wp.backup.domain.database import DatabaseLocation, DumpResult
from l3io.wp.backup.domain.storage_target import StorageTarget
from l3io.wp.backup.ports.archive import ManifestBuilder
from l3io.wp.backup.ports.crypto import PayloadCipher
from l3io.wp.backup.ports.exclusion import ExclusionPolicy
from l3io.wp.backup.ports.tracing import (
    PhaseSpan,
    Redactor,
    RunSpan,
    SpanAttributes,
    Tracer,
)

__all__ = [
    "ActionLog",
    "GuardedTracer",
    "NoopArchiveStore",
    "NoopArchiveWriter",
    "NoopDatabase",
    "NoopTracer",
    "SkippedCall",
]

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


# --------------------------------------------------------------------------
# No-op command adapters (AD-4)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SkippedCall:
    """One command a no-op adapter stood in for."""

    adapter: str
    method: str
    target: str | None = None


class ActionLog:
    """The commands a Dry Run did not perform, in the order they were reached."""

    __slots__ = ("_calls",)

    def __init__(self) -> None:
        self._calls: list[SkippedCall] = []

    def __repr__(self) -> str:
        return f"ActionLog({len(self._calls)} calls)"

    def record(self, adapter: str, method: str, target: str | None = None) -> None:
        """Append one skipped call."""
        self._calls.append(SkippedCall(adapter, method, target))

    @property
    def calls(self) -> tuple[SkippedCall, ...]:
        """Every call recorded so far."""
        return tuple(self._calls)


class NoopDatabase:
    """``DatabaseCommand``: dumps nothing and writes nothing."""

    __slots__ = ("_log",)

    def __init__(self, log: ActionLog) -> None:
        self._log = log

    def dump_to(self, location: DatabaseLocation, destination: Path) -> DumpResult:
        """A zero-byte result; ``destination`` is not created."""
        del destination
        with join_phase(BackupStep.DUMP.value) as record:
            record.skip()
        self._log.record("NoopDatabase", "dump_to", location.name)
        return DumpResult(bytes=0, duration_ms=0.0)


class NoopArchiveWriter:
    """``ArchiveWriterCommand``: collects, encrypts and writes nothing."""

    __slots__ = ("_log",)

    def __init__(self, log: ActionLog) -> None:
        self._log = log

    def write(
        self,
        out: IO[bytes],
        *,
        site_root: Path | None,
        dump: Path | None,
        config: Path,
        exclusion: ExclusionPolicy,
        cipher: PayloadCipher | None,
        manifest_builder: ManifestBuilder,
        workspace: Path,
    ) -> Manifest:
        """The Manifest of an empty index; nothing is read or written."""
        del out, dump, config, exclusion, workspace
        if site_root is not None:
            with phase(BackupStep.COLLECT_SITE_ROOT.value) as record:
                record.skip()
        if cipher is not None:
            with phase(BackupStep.ENCRYPT.value) as record:
                record.skip()
        with phase(BackupStep.WRITE_ARCHIVE.value) as record:
            record.skip()
        self._log.record("NoopArchiveWriter", "write")
        return manifest_builder((), ())


class _NoopStagedArchive:
    """A staged Archive that is never written, verified or published."""

    __slots__ = ("_log", "_stream", "_target")

    def __init__(self, log: ActionLog, target: str) -> None:
        self._log = log
        self._target = target
        self._stream: IO[bytes] = Path(os.devnull).open("wb")  # noqa: SIM115

    @property
    def stream(self) -> IO[bytes]:
        """An ``os.devnull`` writer: nothing accumulates (AD-21)."""
        return self._stream

    def publish(self) -> bool:
        """``False``: not performed. No verifier is called."""
        self._stream.close()
        with phase(BackupStep.VERIFY.value) as record:
            record.skip()
        with phase(BackupStep.PUBLISH.value) as record:
            record.skip()
        self._log.record("NoopArchiveStore", "publish", self._target)
        return False

    def discard(self) -> None:
        """Nothing was staged; the sink is closed."""
        self._stream.close()


class NoopArchiveStore:
    """``ArchiveStoreCommand``: stages nothing at the Storage Target."""

    __slots__ = ("_log",)

    def __init__(self, log: ActionLog) -> None:
        self._log = log

    def stage(self, target: StorageTarget) -> _NoopStagedArchive:
        """A staged Archive whose stream discards every byte."""
        self._log.record("NoopArchiveStore", "stage", str(target.path))
        return _NoopStagedArchive(self._log, str(target.path))
