"""AD-4/AD-24 — the no-op command adapters, the inert tracer and its guard.

**No-op command adapters (AD-4).** One per ``*Command`` Protocol under
``ports/``: :class:`NoopDatabase` (``DatabaseCommand``, both halves),
:class:`NoopArchiveWriter` (``ArchiveWriterCommand``),
:class:`NoopArchiveStore` (``ArchiveStoreCommand``), and the restore twins
:class:`NoopSiteFiles` (``SiteFilesCommand``), :class:`NoopConfigWriter`
(``ConfigCommand``), :class:`NoopSearchReplace` (``SearchReplaceCommand``) and
:class:`NoopRunJournal` (``RunJournalCommand``). A Dry Run binds them in
place of the real command halves (``cli.adapter_sets.dry_run_adapters``); the
query halves stay real. Each performs no effect, records the call into the
:class:`ActionLog` it was built with, emits ``phase_completed`` with
``outcome="skipped_dry_run"`` and ``bytes=0`` (joining the step's phase when the
executor has one open) and returns a value of its declared type that lets the
plan continue. The no-op writer reads no Site Root file and calls no exclusion
policy (ADR-0019); the no-op store's staged Archive is an ``os.devnull`` writer
whose ``publish()`` returns ``False`` and calls no verifier (ADR-0022).
A restore twin's result says ``performed=False`` (ADR-0022); it records
``keep``, ``exclude``, the **keys** of a config write — never its values — and
the journal event, so a restore Dry Run's log shows each skipped effect.
:class:`NoopDatabase`'s restore methods make the same AD-29 administrator
check as the real adapter, so a Dry Run refuses where the real Run would.

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
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, Final, TypeVar

import structlog

from l3io.wp.backup.adapters.config_writer import refuse_unkeepable
from l3io.wp.backup.adapters.database import ADMIN_FLAGS, ADMIN_SOURCE
from l3io.wp.backup.adapters.phase import SKIPPED_OUTCOME, join_open_phase, phase
from l3io.wp.backup.adapters.site_files import refuse_unsafe_clean_target
from l3io.wp.backup.core.backup import BackupStep
from l3io.wp.backup.domain.archive_format import (
    ArchiveView,
    ExtractResult,
    Manifest,
    MemberRole,
)
from l3io.wp.backup.domain.config import ConfigWriteResult, ConstantValue
from l3io.wp.backup.domain.database import (
    DatabaseLocation,
    DumpResult,
    EnsureOutcome,
    LoadResult,
)
from l3io.wp.backup.domain.errors import AdminCredentialsRequiredError, RunTerminated
from l3io.wp.backup.domain.journal import JournalHeader
from l3io.wp.backup.domain.restore import RestorePhase
from l3io.wp.backup.domain.search_replace import (
    UNRESOLVED_URL,
    Replacement,
    SearchReplaceReport,
    UrlRelocation,
)
from l3io.wp.backup.domain.storage_target import StorageTarget
from l3io.wp.backup.ports.archive import ManifestBuilder
from l3io.wp.backup.ports.crypto import PayloadCipher
from l3io.wp.backup.ports.exclusion import ExclusionPolicy
from l3io.wp.backup.ports.site_files import require_site_relative, require_top_level
from l3io.wp.backup.ports.tracing import (
    PhaseSpan,
    Redactor,
    RunSpan,
    SpanAttributes,
    Tracer,
)

__all__ = [
    "JOURNAL_PHASE",
    "ActionLog",
    "GuardedTracer",
    "NoopArchiveStore",
    "NoopArchiveWriter",
    "NoopConfigWriter",
    "NoopDatabase",
    "NoopRunJournal",
    "NoopSearchReplace",
    "NoopSiteFiles",
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
        except RunTerminated:
            raise  # a SIGTERM is the Run's, never the tracer's failure (BL-E002-092)
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


def _skip(phase_name: str, **fields: object) -> None:
    """Mark the open phase ``skipped_dry_run``; ``phase_name`` only if none is open."""
    with join_open_phase(phase_name) as record:
        record.skip()
        record.fields.update(fields)


def _names(paths: Iterable[PurePosixPath]) -> list[str]:
    return sorted(str(p) for p in paths)


class NoopDatabase:
    """``DatabaseCommand``: dumps, creates, loads, revokes and drops nothing.

    ``administrator`` is whether the real adapter it stands in for has an
    administrator bound: the restore methods other than ``load_dump`` refuse
    without one, as the real ones do (AD-29).
    """

    __slots__ = ("_administrator", "_log")

    def __init__(self, log: ActionLog, *, administrator: bool = False) -> None:
        self._log = log
        self._administrator = administrator

    def _require_admin(self) -> None:
        if not self._administrator:
            raise AdminCredentialsRequiredError(ADMIN_SOURCE, flags=ADMIN_FLAGS)

    def dump_to(self, location: DatabaseLocation, destination: Path) -> DumpResult:
        """A zero-byte result; ``destination`` is not created."""
        del destination
        with join_open_phase(BackupStep.DUMP.value) as record:
            record.skip()
        self._log.record("NoopDatabase", "dump_to", location.name)
        return DumpResult(bytes=0, duration_ms=0.0)

    def ensure_database(self, location: DatabaseLocation) -> EnsureOutcome:
        """Nothing created, granted or set."""
        self._require_admin()
        _skip(RestorePhase.LOAD_DUMP.name, method="ensure_database")
        self._log.record("NoopDatabase", "ensure_database", location.name)
        return EnsureOutcome(
            created_database=False,
            created_user=False,
            password_set=False,
            granted=False,
            warnings=(SKIPPED_OUTCOME,),
            performed=False,
        )

    def load_dump(
        self, location: DatabaseLocation, source: IO[bytes] | Path
    ) -> LoadResult:
        """Nothing loaded; ``source`` is not read."""
        del source
        _skip(RestorePhase.LOAD_DUMP.name, method="load_dump")
        self._log.record("NoopDatabase", "load_dump", location.name)
        return LoadResult(bytes=0, duration_ms=0.0, performed=False)

    def revoke_grants(
        self, location: DatabaseLocation, *, user: str, confirm: str
    ) -> bool:
        """``False``: nothing revoked."""
        del confirm
        self._require_admin()
        _skip(RestorePhase.CLEAN.name, method="revoke_grants", user=user)
        self._log.record("NoopDatabase", "revoke_grants", location.name)
        return False

    def drop_database(self, location: DatabaseLocation, *, confirm: str) -> bool:
        """``False``: nothing dropped."""
        del confirm
        self._require_admin()
        _skip(RestorePhase.CLEAN.name, method="drop_database")
        self._log.record("NoopDatabase", "drop_database", location.name)
        return False


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

    @property
    def staged_path(self) -> Path | None:
        """``None``: nothing is staged on disk."""
        return None

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


class NoopSiteFiles:
    """``SiteFilesCommand``: removes and extracts nothing."""

    __slots__ = ("_log",)

    def __init__(self, log: ActionLog) -> None:
        self._log = log

    def clean(self, site_root: Path, *, keep: frozenset[PurePosixPath]) -> int:
        """``0``: nothing removed. ``keep`` and the target are checked as the
        real one checks them, so an unsafe target is refused here too."""
        require_site_relative(keep, argument="keep")
        require_top_level(keep, argument="keep")
        refuse_unsafe_clean_target(site_root.absolute())
        _skip(RestorePhase.CLEAN.name, method="clean", keep=_names(keep))
        self._log.record("NoopSiteFiles", "clean", str(site_root))
        return 0

    def extract(
        self,
        source: StorageTarget,
        destination: Path,
        view: ArchiveView,
        *,
        roles: frozenset[MemberRole],
        exclude: frozenset[PurePosixPath] = frozenset(),
        payload: Path | None = None,
    ) -> ExtractResult:
        """Nothing read or written; ``exclude`` is checked as the real one checks it."""
        del source, view, payload
        require_site_relative(exclude, argument="exclude")
        _skip(
            RestorePhase.EXTRACT.name,
            method="extract",
            roles=sorted(r.value for r in roles),
            exclude=_names(exclude),
        )
        self._log.record("NoopSiteFiles", "extract", str(destination))
        return ExtractResult(members=0, bytes=0, skipped_links=())


class NoopConfigWriter:
    """``ConfigCommand``: writes, copies and replaces nothing.

    Records constant **names** only: ``set_constants`` may be handed a
    ``DB_PASSWORD`` value (relocation), and it must never reach a log.
    """

    __slots__ = ("_log",)

    def __init__(self, log: ActionLog) -> None:
        self._log = log

    def _skipped(self, method: str, target: Path, keys: Iterable[str]) -> None:
        _skip(RestorePhase.REWRITE_CONFIG.name, method=method, keys=sorted(keys))
        self._log.record("NoopConfigWriter", method, str(target))

    def set_constants(
        self, config_file: Path, values: Mapping[str, str | int | bool]
    ) -> ConfigWriteResult:
        """Nothing changed; the keys of ``values`` are recorded, not the values."""
        self._skipped("set_constants", config_file, values.keys())
        return ConfigWriteResult(changed=(), performed=False)

    def save_copy(self, config_file: Path, destination: Path) -> ConfigWriteResult:
        """Nothing copied; ``destination`` is not created."""
        del config_file
        self._skipped("save_copy", destination, ())
        return ConfigWriteResult(changed=(), performed=False)

    def replace_file(
        self,
        config_file: Path,
        content: str,
        *,
        keep: Mapping[str, ConstantValue],
    ) -> ConfigWriteResult:
        """Nothing replaced and nothing written; the keys of ``keep`` are recorded.

        A ``None`` in ``keep`` is refused as the real writer refuses it. That
        ``config_file`` exists is not checked: in a Dry Run EXTRACT wrote
        nothing, so its absence here says nothing about the real Run.
        """
        del content
        refuse_unkeepable(config_file, keep)
        self._skipped("replace_file", config_file, keep.keys())
        return ConfigWriteResult(changed=(), performed=False)


class NoopSearchReplace:
    """``SearchReplaceCommand``: replaces nothing and reads nothing.

    The planned pairs carry :data:`UNRESOLVED_URL` as ``old`` and every count
    is ``None``: the stored URLs live in a database a Dry Run did not load.
    """

    __slots__ = ("_log",)

    def __init__(self, log: ActionLog) -> None:
        self._log = log

    def relocate_content(
        self,
        site_root: Path,
        urls: UrlRelocation,
        *,
        archive_encrypted: bool | None = None,
    ) -> SearchReplaceReport:
        """The planned pairs, ``old`` unresolved; nothing replaced."""
        del archive_encrypted
        targets = (urls.site_url, urls.home_url)
        news = list(dict.fromkeys(u for u in targets if u is not None))
        _skip(RestorePhase.SEARCH_REPLACE.name, method="relocate_content")
        self._log.record("NoopSearchReplace", "relocate_content", str(site_root))
        return SearchReplaceReport(
            pairs=tuple(Replacement(old=UNRESOLVED_URL, new=new) for new in news),
            replacements=None,
            tables=None,
            remaining=None,
            merged=False,
        )


#: The ``phase`` of a skipped journal call's ``phase_completed`` record. A
#: journal call is not a restore phase's effect, so it never joins one; the
#: restore phase it concerns is the record's ``journal_phase`` field.
JOURNAL_PHASE: Final = "JOURNAL"


class NoopRunJournal:
    """``RunJournalCommand``: records nothing, so a Dry Run resolves nothing."""

    __slots__ = ("_log",)

    def __init__(self, log: ActionLog) -> None:
        self._log = log

    def _skipped(self, event: str, target: str | None = None, **fields: object) -> None:
        # ``journal_event``: ``event`` is the log record's own name.
        _skip(JOURNAL_PHASE, journal_event=event, **fields)
        self._log.record("NoopRunJournal", event, target)

    def begin_run(self, header: JournalHeader) -> None:
        """Nothing written."""
        self._skipped("begin_run", header.run_id)

    def phase_started(self, phase: RestorePhase, target: str) -> None:
        """Nothing written."""
        self._skipped("phase_started", target, journal_phase=phase.value)

    def step_completed(self, phase: RestorePhase, step: str) -> None:
        """Nothing written."""
        self._skipped("step_completed", step, journal_phase=phase.value)

    def phase_completed(self, phase: RestorePhase) -> None:
        """Nothing written."""
        self._skipped("phase_completed", None, journal_phase=phase.value)

    def finish_run(self) -> None:
        """Nothing written."""
        self._skipped("finish_run")

    def resolve(self, run_id: str, resolved_by: str) -> None:
        """Nothing resolved (ADR-0021)."""
        self._skipped("resolve", run_id, resolved_by=resolved_by)
