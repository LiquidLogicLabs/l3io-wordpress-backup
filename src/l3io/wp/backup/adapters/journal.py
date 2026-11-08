"""AD-18.1, ADR-0020, ADR-0021 — the Run Journal on disk (E003-S02-005).

:class:`FileRunJournal` implements ``ports/journal.py``'s
``RunJournalQuery`` and ``RunJournalCommand`` over ``domain/journal.py``'s
types. It owns the file layout and the durable write sequence:

* the per-Site directory (:func:`journal_directory`) is
  ``platformdirs.user_state_path("l3io-wp-backup") / "snapshots" /
  sha256(canonical Site Root)``, mode ``0700``; it never follows
  ``--snapshot-dir`` and is never inside the Site Root (ADR-0021);
* ``current.json`` is the one blocking record — the whole
  :class:`~l3io.wp.backup.domain.journal.JournalRecord`, rewritten on every
  event: ``json.dumps``, then ``current.json.tmp`` opened ``O_CREAT|O_TRUNC``
  at mode ``0600`` and ``os.fsync``-ed, then ``os.replace`` onto
  ``current.json`` and ``os.fsync`` on the directory. A record on disk is
  therefore the previous or the new version, durable before the phase it
  announces begins;
* ``finish_run`` moves it to ``completed/<run_id>.json`` when every started
  phase completed, else to ``failed/<run_id>.json``; ``resolve`` moves it to
  ``resolved/<run_id>.json`` with ``resolved_at``/``resolved_by``.

The journal holds names, paths, the Run ID, the Mode and step names only;
``JournalHeader`` refuses ``DB_PASSWORD``. A journal that cannot be read back
refuses (:class:`CorruptJournalError`); it is never treated as absent.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Final, cast, get_args

import structlog

from l3io.wp.backup.adapters.lock import state_root
from l3io.wp.backup.adapters.workspace import private_state_directories
from l3io.wp.backup.core.restore import IncompleteRunError, incomplete_run_error
from l3io.wp.backup.domain.errors import (
    BackupError,
    RefusedError,
    UnknownInterruptedRunError,
)
from l3io.wp.backup.domain.journal import (
    JournalEntry,
    JournalEvent,
    JournalHeader,
    JournalRecord,
)
from l3io.wp.backup.domain.restore import RestoreMode, RestorePhase

__all__ = [
    "CURRENT",
    "CorruptJournalError",
    "FileRunJournal",
    "JournalWriteError",
    "journal_directory",
    "record_from_json",
    "record_to_json",
    "refuse_incomplete",
]

_log = structlog.get_logger(__name__)

#: The one blocking record in a Site's journal directory.
CURRENT: Final = "current.json"
_TMP: Final = "current.json.tmp"
_COMPLETED: Final = "completed"
_FAILED: Final = "failed"
_RESOLVED: Final = "resolved"
_DIR_MODE: Final = 0o700
_FILE_MODE: Final = 0o600
#: A record is a few kilobytes; anything far larger is not one.
_RECORD_LIMIT: Final = 1024 * 1024
_EVENTS: Final = frozenset(get_args(JournalEvent))


class JournalWriteError(BackupError):
    """A journal record could not be made durable; the phase does not begin."""

    def __init__(self, path: Path, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(
            f"could not write the run journal {str(path)!r}: {reason}; "
            "the next phase was not started"
        )


class CorruptJournalError(RefusedError):
    """``current.json`` exists and cannot be read back (NFR-1: never absent).

    ``--resolve-interrupted-run`` cannot act on it either, since no Run ID or
    phase can be read; the message names the manual way out (R10).
    """

    def __init__(self, journal_path: str, reason: str) -> None:
        self.journal_path = journal_path
        self.reason = reason
        super().__init__(
            f"an incomplete run journal {journal_path!r} could not be read "
            f"({reason}), so the interrupted phase could not be determined; "
            "inspect the Site and that file, repair the Site if needed (a "
            "Safety Snapshot taken to the default directory is beside it), "
            "then move that file aside (for example rename it with a .corrupt "
            "suffix) before restoring again"
        )


def journal_directory(site_root: Path) -> Path:
    """The per-Site journal directory for the canonical ``site_root`` (ADR-0021).

    ``<user state dir>/snapshots/<sha256 of the canonical path>``: story 3.3's
    default snapshot directory, beside E002-S02-008's ``locks/``.
    """
    canonical = Path(site_root).resolve(strict=True)
    digest = hashlib.sha256(str(canonical).encode()).hexdigest()
    return state_root() / "snapshots" / digest


# --------------------------------------------------------------------------
# Serialisation — json.dumps of plain values, never string formatting
# --------------------------------------------------------------------------


def _header_dict(header: JournalHeader) -> dict[str, object]:
    return {
        "run_id": header.run_id,
        "started_at": header.started_at,
        "site_root": str(header.site_root),
        "restore_mode": header.restore_mode.value,
        "archive": header.archive,
        "snapshot_path": header.snapshot_path,
        "live_database_before": header.live_database_before,
        "staging_database": header.staging_database,
        "wp_config_path": header.wp_config_path,
        "load_database": header.load_database,
        "cutover_keys": list(header.cutover_keys),
        "cutover_targets": dict(header.cutover_targets),
        "prior_constants": dict(header.prior_constants),
        "phase_undo": {p.value: t for p, t in header.phase_undo.items()},
        "saved_config": header.saved_config,
    }


def _entry_dict(entry: JournalEntry) -> dict[str, object]:
    return {
        "event": entry.event,
        "phase": entry.phase.value,
        "at": entry.at,
        "target": entry.target,
        "step": entry.step,
    }


def record_to_json(record: JournalRecord, **extra: object) -> str:
    """``record`` as the JSON document ``current.json`` holds."""
    body: dict[str, object] = {
        "header": _header_dict(record.header),
        "entries": [_entry_dict(e) for e in record.entries],
        **extra,
    }
    return json.dumps(body, indent=2, sort_keys=True)


def _str(value: object, name: str) -> str:
    if not isinstance(value, str):
        msg = f"{name} is not a string"
        raise TypeError(msg)
    return value


def _opt(value: object, name: str) -> str | None:
    return None if value is None else _str(value, name)


def _header(raw: Mapping[str, Any]) -> JournalHeader:
    return JournalHeader(
        run_id=_str(raw["run_id"], "run_id"),
        started_at=_str(raw["started_at"], "started_at"),
        site_root=PurePosixPath(_str(raw["site_root"], "site_root")),
        restore_mode=RestoreMode(raw["restore_mode"]),
        archive=_str(raw["archive"], "archive"),
        snapshot_path=_opt(raw["snapshot_path"], "snapshot_path"),
        live_database_before=_str(raw["live_database_before"], "live_database"),
        staging_database=_opt(raw["staging_database"], "staging_database"),
        wp_config_path=_str(raw["wp_config_path"], "wp_config_path"),
        load_database=_str(raw["load_database"], "load_database"),
        cutover_keys=tuple(_str(k, "cutover key") for k in raw["cutover_keys"]),
        cutover_targets={
            _str(k, "key"): _str(v, "target") for k, v in raw["cutover_targets"].items()
        },
        prior_constants={
            _str(k, "key"): _opt(v, "prior") for k, v in raw["prior_constants"].items()
        },
        phase_undo={
            RestorePhase(k): _str(v, "undo") for k, v in raw["phase_undo"].items()
        },
        saved_config=_opt(raw["saved_config"], "saved_config"),
    )


def _entry(raw: Mapping[str, Any]) -> JournalEntry:
    event = raw["event"]
    if event not in _EVENTS:
        msg = f"unknown journal event {event!r}"
        raise ValueError(msg)
    return JournalEntry(
        event=cast("JournalEvent", event),
        phase=RestorePhase(raw["phase"]),
        at=_str(raw["at"], "at"),
        target=_opt(raw["target"], "target"),
        step=_opt(raw["step"], "step"),
    )


def record_from_json(text: str) -> JournalRecord:
    """The record ``text`` holds; ``ValueError``/``KeyError``/``TypeError`` if not."""
    body = json.loads(text)
    if not isinstance(body, dict):
        msg = "the journal is not a JSON object"
        raise TypeError(msg)
    entries = body["entries"]
    if not isinstance(entries, list):
        msg = "entries is not a list"
        raise TypeError(msg)
    return JournalRecord(
        header=_header(body["header"]), entries=tuple(_entry(e) for e in entries)
    )


# --------------------------------------------------------------------------
# The durable write sequence
# --------------------------------------------------------------------------


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_atomically(
    directory: Path, name: str, text: str, *, exclusive: bool = False
) -> Path:
    """``text`` to ``directory/name``: temp at ``0600``, fsync, replace, fsync dir.

    With ``exclusive`` the temp file is published by ``os.link``, which fails
    with ``FileExistsError`` when ``name`` exists, so an existing record is
    never replaced: the check and the write are one system call (BL-E003-077).
    """
    final = directory / name
    tmp = directory / f"{name}.tmp" if name != CURRENT else directory / _TMP
    data = text.encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    fd = os.open(tmp, flags, _FILE_MODE)
    try:
        os.fchmod(fd, _FILE_MODE)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    if exclusive:
        try:
            os.link(tmp, final)  # link(2): EEXIST rather than replace
        finally:
            tmp.unlink()
    else:
        tmp.replace(final)  # os.replace(2): atomic within the directory
    _fsync_directory(directory)
    return final


def _ensure_dir(path: Path) -> None:
    private_state_directories(path, root=state_root())  # BL-E008-027
    path.chmod(_DIR_MODE)


class FileRunJournal:
    """``RunJournalQuery`` and ``RunJournalCommand`` over one Site's directory.

    Constructed by the restore root with :func:`journal_directory`'s path.
    ``begin_run`` writes the header with no entries; each ``phase_started``,
    ``step_completed`` and ``phase_completed`` appends one entry and rewrites
    the record durably. A failed write raises :class:`JournalWriteError`, so
    the phase it would announce never begins.
    """

    __slots__ = ("_directory", "_record")

    def __init__(self, directory: Path) -> None:
        self._directory = Path(directory)
        self._record: JournalRecord | None = None

    def __repr__(self) -> str:
        return f"FileRunJournal({str(self._directory)!r})"

    @property
    def directory(self) -> Path:
        return self._directory

    @property
    def journal_path(self) -> Path:
        """Where the blocking record lives: ``<directory>/current.json``."""
        return self._directory / CURRENT

    # ------------------------------------------------------------------
    # RunJournalQuery
    # ------------------------------------------------------------------

    def incomplete(self, site_root: PurePosixPath) -> JournalRecord | None:
        """``current.json``'s record, or ``None``; unreadable refuses.

        ``site_root`` is already this directory's key (the root hashed it),
        so a journal for another Site is never in it.
        """
        del site_root
        path = self.journal_path
        try:
            with path.open("rb") as handle:
                data = handle.read(_RECORD_LIMIT + 1)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise CorruptJournalError(
                str(path), exc.strerror or type(exc).__name__
            ) from exc
        if len(data) > _RECORD_LIMIT:
            raise CorruptJournalError(str(path), "larger than any journal record")
        try:
            return record_from_json(data.decode("utf-8"))
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise CorruptJournalError(str(path), type(exc).__name__) from exc

    # ------------------------------------------------------------------
    # RunJournalCommand
    # ------------------------------------------------------------------

    def _write(
        self, record: JournalRecord, entry: JournalEntry | None, *, first: bool = False
    ) -> None:
        text = record_to_json(record)
        try:
            _ensure_dir(self._directory)
            _write_atomically(self._directory, CURRENT, text, exclusive=first)
        except FileExistsError as exc:
            raise JournalWriteError(
                self.journal_path, "an unresolved journal is already present"
            ) from exc
        except OSError as exc:
            raise JournalWriteError(
                self.journal_path, exc.strerror or type(exc).__name__
            ) from exc
        self._record = record
        _log.debug(
            "journal_written",
            journal_run_id=record.header.run_id,
            phase=None if entry is None else entry.phase.name,
            journal_event="begin" if entry is None else _event_name(entry),
            step=None if entry is None else entry.step,
            journal_path=str(self.journal_path),
        )

    def _current(self) -> JournalRecord:
        if self._record is None:
            msg = "the run journal was not begun"
            raise ValueError(msg)
        return self._record

    def _append(
        self,
        event: JournalEvent,
        phase: RestorePhase,
        *,
        target: str | None = None,
        step: str | None = None,
    ) -> None:
        record = self._current()
        entry = JournalEntry(
            event=event,
            phase=phase,
            at=datetime.now(UTC).isoformat(),
            target=target,
            step=step,
        )
        self._write(
            JournalRecord(header=record.header, entries=(*record.entries, entry)),
            entry,
        )

    def begin_run(self, header: JournalHeader) -> None:
        """Write ``header`` with no entries; an existing record is never replaced.

        Exclusive by ``link(2)``, not by a check before the write, so it holds
        even without the Site Lock (BL-E003-077).
        """
        self._write(JournalRecord(header=header, entries=()), None, first=True)

    def phase_started(self, phase: RestorePhase, target: str) -> None:
        self._append("phase_started", phase, target=target)

    def step_completed(self, phase: RestorePhase, step: str) -> None:
        self._append("step_completed", phase, step=step)

    def phase_completed(self, phase: RestorePhase) -> None:
        self._append("phase_completed", phase)

    def finish_run(self) -> None:
        """Move ``current.json`` to ``completed/`` or ``failed/``; fsync both dirs."""
        record = self._current()
        started = {e.phase for e in record.entries if e.event == "phase_started"}
        completed = {e.phase for e in record.entries if e.event == "phase_completed"}
        folder = _COMPLETED if started <= completed else _FAILED
        self._move(record.header.run_id, folder, None)
        self._record = None

    def resolve(self, run_id: str, resolved_by: str) -> None:
        """Move ``run_id``'s blocking record to ``resolved/``, acknowledged.

        Refuses (``UnknownInterruptedRunError``) when no journal blocks, or when the one
        that does names another Run: a blanket acknowledgement resolves
        nothing it did not name.
        """
        record = self.incomplete(PurePosixPath(self._directory))
        if record is None:
            raise UnknownInterruptedRunError(run_id, recorded_run_id=None)
        if record.header.run_id != run_id:
            raise UnknownInterruptedRunError(
                run_id, recorded_run_id=record.header.run_id
            )
        extra: dict[str, object] = {
            "resolved_at": datetime.now(UTC).isoformat(),
            "resolved_by": resolved_by,
        }
        self._record = record
        self._move(run_id, _RESOLVED, extra)
        self._record = None

    def _move(self, run_id: str, folder: str, extra: dict[str, object] | None) -> None:
        record = self._current()
        destination = self._directory / folder
        try:
            _ensure_dir(destination)
            if extra is None:
                self.journal_path.replace(destination / f"{run_id}.json")
            else:
                text = record_to_json(record, **extra)
                _write_atomically(destination, f"{run_id}.json", text)
                self.journal_path.unlink()
            _fsync_directory(destination)
            _fsync_directory(self._directory)
        except OSError as exc:
            raise JournalWriteError(
                self.journal_path, exc.strerror or type(exc).__name__
            ) from exc
        _log.debug(
            "journal_written",
            journal_run_id=run_id,
            phase=None,
            journal_event=folder,
            step=None,
            journal_path=str(destination / f"{run_id}.json"),
        )


def _event_name(entry: JournalEntry) -> str:
    match entry.event:
        case "phase_started":
            return "started"
        case "phase_completed":
            return "completed"
        case "step_completed":
            return "step"


def refuse_incomplete(journal: FileRunJournal, site_root: PurePosixPath) -> None:
    """Raise :class:`IncompleteRunError` when ``site_root`` has a blocking journal.

    The query the restore root runs after the Site Lock and before
    ``PREFLIGHT`` (so a Dry Run refuses too); ``incomplete_run_found`` is
    logged at ERROR with the error's fields.
    """
    record = journal.incomplete(site_root)
    if record is None:
        return
    error: IncompleteRunError = incomplete_run_error(record, str(journal.journal_path))
    _log.error(
        "incomplete_run_found",
        run_id_interrupted=error.run_id,
        started_at=error.started_at,
        interrupted_phase=None
        if error.interrupted_phase is None
        else error.interrupted_phase.value,
        undo=error.undo,
        journal_path=error.journal_path,
        written_keys=list(error.written_keys),
        pending_keys=list(error.pending_keys),
    )
    raise error
