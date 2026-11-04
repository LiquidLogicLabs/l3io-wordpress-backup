"""AD-18.2, AD-18.3, ADR-0020, ADR-0022 — the staged load and the cutover.

:func:`load_staged` is the real ``load_database`` slot of
``adapters/restore_runner.py``'s ``execute_restore``, and :func:`check_staged`
its ``check_database`` slot (the database part of ``PREFLIGHT``). The root
(E003-S02-006) binds both with ``functools.partial`` over the Run's
:class:`~l3io.wp.backup.ports.restore.RestorePorts`; a Dry Run is the same
calls over ``dry_run_adapters(...)``, so this module has no branch on it.

``load_staged`` performs, in order, journaling each step through the journal
it is handed:

1. ``ensure_database`` of the load location — the Run's staging database
   (``core/restore.py::staging_name``) or the operator's ``--new-db-name``;
2. ``load_dump`` of the plain Dump, extracted into the Run workspace;
3. the post-load check, ``survey_target`` of the loaded database
   (``"check_failed"`` and :class:`StagingCheckFailedError` when it is not
   WordPress); a not-performed ``LoadResult`` (a Dry Run) surveys nothing;
4. the cutover — the only place the live connection changes: one
   ``ConfigCommand.set_constants(config_file, {key: value})`` per key, each
   preceded by ``"cutover_started:<KEY>"`` and followed by ``"cutover:<KEY>"``;
5. the loser: for a Mode that removes the database on the same server,
   ``"loser_drop_started"``, ``revoke_grants``, ``drop_database`` and
   ``"loser_dropped"``; otherwise the previous database is retained.

A failure before the cutover drops the staging database this Run created
(``"staging_dropped"``); an operator-named database that already existed is
left in place. No SQL is issued and no config byte is written here: database
and ``adapters/config_writer`` do both. ``l3io.wp.database`` is imported only
inside :func:`_validate_name` (AD-8), so this module imports without the
``database`` extra; it imports nothing from ``l3io.wp.config``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

import structlog

from l3io.wp.backup.adapters.database import ADMIN_SOURCE, blocked_dump_error
from l3io.wp.backup.core.restore import (
    CUTOVER_KEYS,
    STEP_CHECK_FAILED,
    STEP_CUTOVER,
    STEP_CUTOVER_READBACK_FAILED,
    STEP_CUTOVER_STARTED,
    STEP_LOSER_DROP_STARTED,
    STEP_LOSER_DROPPED,
    STEP_STAGING_DROPPED,
    DatabaseRestoreReport,
    RestoreInputs,
    cutover_progress,
    database_report_fields,
    database_restored,
    interrupted_phase,
    last_step,
    load_names,
    planned_cutover_keys,
    planned_cutover_targets,
)
from l3io.wp.backup.domain.archive_format import DUMP_RELATIVE, MemberRole
from l3io.wp.backup.domain.database import (
    DatabaseLocation,
    EnsureOutcome,
    LoadResult,
    Readiness,
)
from l3io.wp.backup.domain.errors import (
    BackupError,
    ConfigWriteFailedError,
    CredentialsError,
    DatabaseRefusedError,
    MissingExtraError,
    ProvisioningFailedError,
    RefusedError,
)
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.domain.journal import JournalRecord
from l3io.wp.backup.domain.restore import RestorePhase, removes_database
from l3io.wp.backup.ports.journal import RunJournalCommand
from l3io.wp.backup.ports.restore import RestorePorts

__all__ = [
    "CUTOVER_PASSWORD_SOURCE",
    "ENSURE_RESOLVES",
    "ConfigCutoverError",
    "InvalidLoadDatabaseNameError",
    "StagingCheckFailedError",
    "check_staged",
    "complete_cutover",
    "load_staged",
]

_log = structlog.get_logger(__name__)

#: ``check_load``'s blockers for the state ``ensure_database`` is about to
#: create (database's ``Code`` values). Allowed only when ``check_ensure`` is
#: ready, because ``PREFLIGHT`` runs before ``ensure_database``.
ENSURE_RESOLVES: Final[frozenset[str]] = frozenset(
    {"database_absent", "user_absent", "authentication_failed"}
)
#: The label a missing relocated ``DB_PASSWORD`` source is named by.
CUTOVER_PASSWORD_SOURCE: Final = "relocated WordPress account"  # noqa: S105 - a label
_PASSWORD: Final = "DB_PASSWORD"  # noqa: S105 - a key name
_PHASE: Final = RestorePhase.LOAD_DUMP
_NS_PER_MS: Final = 1_000_000


class StagingCheckFailedError(BackupError):
    """The loaded database is not a WordPress database; nothing was cut over."""

    def __init__(
        self, database: str, *, is_wordpress: bool, schema_version: str | None
    ) -> None:
        self.database = database
        self.is_wordpress = is_wordpress
        self.schema_version = schema_version
        super().__init__(
            f"the loaded database {database!r} failed its check (is_wordpress="
            f"{is_wordpress}, schema_version={schema_version}); wp-config.php was "
            "not changed"
        )


class ConfigCutoverError(BackupError):
    """A cutover write failed; ``key`` is byte-identical, ``written`` landed."""

    def __init__(
        self, path: str, key: str, *, written: tuple[str, ...], pending: tuple[str, ...]
    ) -> None:
        self.path = path
        self.key = key
        self.written = written
        self.pending = pending
        state = (
            "no key was written"
            if not written
            else f"written: {', '.join(written)}; pending: {', '.join(pending)}"
        )
        super().__init__(f"the cutover could not write {key} to {path!r}; {state}")


class InvalidLoadDatabaseNameError(RefusedError):
    """The load database's name is not a usable database identifier."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(
            f"the load database name {name!r} is not a valid database name; "
            "nothing was changed"
        )


# --------------------------------------------------------------------------
# The staging hook: where the Dump loads (decision 2 of 2026-10-27)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Target:
    live: DatabaseLocation
    load: DatabaseLocation
    staging: str | None
    #: The WordPress account that had the live database (prior ``DB_USER``).
    live_user: str | None
    #: The account the Dump loads as: the relocated user, else the live one.
    load_user: str | None

    @property
    def same_server(self) -> bool:
        return (self.load.host, self.load.port) == (self.live.host, self.live.port)


def _target(inputs: RestoreInputs) -> _Target:
    site, relocation = inputs.site, inputs.relocation
    live = site.database_location()
    load_name, staging = load_names(site.db_name, inputs.run_id, relocation)
    base = relocation.load_location or live
    prior = inputs.header.prior_constants.get("DB_USER")
    return _Target(
        live=live,
        load=base.with_name(load_name),
        staging=staging,
        live_user=prior,
        load_user=relocation.cutover_user or prior,
    )


def _validate_name(name: str) -> None:
    """Database's ``validate_database_name``, before the first call using ``name``."""
    try:
        from l3io.wp import database as wpdb
    except ImportError as exc:
        raise MissingExtraError(extra="database") from exc
    try:
        wpdb.validate_database_name(name)
    except wpdb.InvalidIdentifierError as exc:
        raise InvalidLoadDatabaseNameError(name) from exc


def _require_password_source(inputs: RestoreInputs, ports: RestorePorts) -> None:
    keys = planned_cutover_keys(inputs.relocation)
    if _PASSWORD in keys and ports.cutover_credentials is None:
        raise CredentialsError(CUTOVER_PASSWORD_SOURCE, missing_key=_PASSWORD)


# --------------------------------------------------------------------------
# PREFLIGHT — queries only
# --------------------------------------------------------------------------


def _refuse(location: DatabaseLocation, blockers: tuple[str, ...]) -> None:
    if not blockers:
        return
    error = blocked_dump_error(location, blockers, credential_source=ADMIN_SOURCE)
    raise DatabaseRefusedError(findings=blockers) if error is None else error


def check_staged(inputs: RestoreInputs, *, ports: RestorePorts) -> None:
    """The database part of ``PREFLIGHT``: database's checks, before any command.

    ``check_ensure`` and ``check_load`` of the load location (``source=None``:
    the Dump is not extracted yet), and — when the Mode removes the database
    and the loser is on the same server — ``check_revoke`` and
    ``check_drop_database`` of the live one. Every query is made before any
    result is judged, so a Dry Run makes exactly these calls. When
    ``check_ensure`` is ready, ``check_load``'s :data:`ENSURE_RESOLVES`
    blockers are expected and removed; any remaining blocker refuses.
    """
    target = _target(inputs)
    _validate_name(target.load.name)
    _require_password_source(inputs, ports)
    query = ports.adapters.restore_database_query
    ensure = query.check_ensure(target.load)
    load = query.check_load(target.load, source=None)
    loser: list[tuple[DatabaseLocation, Readiness]] = []
    if (
        removes_database(inputs.header.restore_mode)
        and target.same_server
        and target.load != target.live
    ):
        if target.live_user is not None:
            user = target.live_user
            revoke = query.check_revoke(target.live, user=user, confirm=user)
            loser.append((target.live, revoke))
        drop = query.check_drop_database(target.live, confirm=target.live.name)
        loser.append((target.live, drop))
    _refuse(target.load, ensure.blockers)
    blockers = load.blockers
    if ensure.ready:
        blockers = tuple(b for b in blockers if b not in ENSURE_RESOLVES)
    _refuse(target.load, blockers)
    for location, readiness in loser:
        _refuse(location, readiness.blockers)


# --------------------------------------------------------------------------
# LOAD_DUMP
# --------------------------------------------------------------------------


def _check_header(inputs: RestoreInputs, target: _Target) -> tuple[str, ...]:
    """The cutover keys; ``ValueError`` when the header was built for another load."""
    header = inputs.header
    keys = planned_cutover_keys(inputs.relocation)
    expected = (target.load.name, target.staging, keys)
    found = (header.load_database, header.staging_database, header.cutover_keys)
    if expected != found:
        msg = (
            f"the journal header names load {found[0]!r}, staging {found[1]!r} and "
            f"keys {list(found[2])}; this Run loads {expected[0]!r}, staging "
            f"{expected[1]!r}, keys {list(keys)}"
        )
        raise ValueError(msg)
    return keys


def _drop_staging(
    target: _Target,
    ensure: EnsureOutcome | None,
    *,
    ports: RestorePorts,
    journal: RunJournalCommand,
) -> None:
    """The error path: drop the load database only if this Run created it.

    A failed drop is logged and the journal stays blocking; the caller
    re-raises the original error.
    """
    if ensure is None or not ensure.created_database:
        return
    name = target.load.name
    try:
        ports.adapters.restore_database_command.drop_database(target.load, confirm=name)
        journal.step_completed(_PHASE, STEP_STAGING_DROPPED)
    except Exception as exc:  # the original error is re-raised by the caller
        _log.error(
            "staging_database_drop_failed",
            staging_database=name,
            error_type=type(exc).__name__,
        )
        return
    _log.warning("staging_database_dropped", staging_database=name)


def _post_load_check(
    inputs: RestoreInputs,
    target: _Target,
    loaded: LoadResult,
    *,
    ports: RestorePorts,
    journal: RunJournalCommand,
) -> bool:
    """Structural: the loaded database must be WordPress with a schema version.

    Given a not-performed ``LoadResult`` (a Dry Run's ``NoopDatabase``) it
    surveys nothing, reports "would check" instead (ADR-0022) and returns
    ``False``: nothing was checked. ``True`` only for a check that passed.
    """
    name = target.load.name
    if not loaded.performed:
        _log.info("staging_load_checked", staging_database=name, performed=False)
        _log.info("would_check", staging_database=name)
        return False
    state = ports.adapters.restore_database_query.survey_target(
        target.load,
        table_prefix=inputs.site.table_prefix,
        wordpress_user=target.load_user,
    )
    _log.info(
        "staging_load_checked",
        staging_database=name,
        is_wordpress=state.is_wordpress,
        schema_version=state.schema_version,
        performed=True,
    )
    if state.is_wordpress is not True or state.schema_version is None:
        journal.step_completed(_PHASE, STEP_CHECK_FAILED)
        raise StagingCheckFailedError(
            name, is_wordpress=state.is_wordpress, schema_version=state.schema_version
        )
    return True


def _cutover_value(
    key: str, inputs: RestoreInputs, ports: RestorePorts, load_name: str
) -> str:
    if key == _PASSWORD:
        if ports.cutover_credentials is None:
            raise CredentialsError(CUTOVER_PASSWORD_SOURCE, missing_key=_PASSWORD)
        return ports.cutover_credentials()
    return planned_cutover_targets(load_name, inputs.relocation)[key]


def _cutover(
    inputs: RestoreInputs,
    target: _Target,
    keys: tuple[str, ...],
    ensure: EnsureOutcome,
    *,
    ports: RestorePorts,
    journal: RunJournalCommand,
) -> tuple[str, ...]:
    """One atomic, verified ``set_constants`` per key, each journaled (AD-18.2).

    Returns the keys written, in order.
    """
    config_file = Path(inputs.relocation.config_file or inputs.config_path)
    command = ports.adapters.config_command
    started = time.monotonic_ns()
    landed: list[str] = []
    for key in keys:
        journal.step_completed(_PHASE, STEP_CUTOVER_STARTED + key)
        value: str | None = _cutover_value(key, inputs, ports, target.load.name)
        try:
            command.set_constants(config_file, {key: value or ""})
        except ConfigWriteFailedError as exc:
            if not landed:
                journal.step_completed(_PHASE, STEP_CUTOVER_READBACK_FAILED)
                _drop_staging(target, ensure, ports=ports, journal=journal)
            pending = tuple(k for k in keys if k not in landed)
            raise ConfigCutoverError(
                str(config_file), key, written=tuple(landed), pending=pending
            ) from exc
        finally:
            value = None  # DB_PASSWORD is held for its one write only
        landed.append(key)
        journal.step_completed(_PHASE, STEP_CUTOVER + key)
        _log.debug("cutover_key_written", key=key, wp_config_path=str(config_file))
    host_fields: dict[str, object] = {}
    if inputs.relocation.cutover_host is not None:
        host_fields = {
            "host_from": target.live.host,
            "host_to": inputs.relocation.cutover_host,
        }
    _log.info(
        "database_cutover",
        keys=list(keys),
        wp_config_path=str(config_file),
        **{"from": target.live.name, "to": target.load.name},
        **host_fields,
    )
    _log.info(
        "phase_completed",
        phase=_PHASE.value,
        step="cutover",
        duration_ms=(time.monotonic_ns() - started) / _NS_PER_MS,
        bytes=0,
        outcome="ok",
        exit_code=int(ExitCode.OK),
    )
    return tuple(landed)


def _loser(
    inputs: RestoreInputs,
    target: _Target,
    *,
    ports: RestorePorts,
    journal: RunJournalCommand,
    performed: bool,
) -> Literal["dropped", "retained", "none", "absent"]:
    """What happens to the database that lost the cutover (AD-18.3).

    ``"none"`` when the administrator's survey found no live database, or a
    performed drop found nothing to remove; a Dry Run's not-performed drop
    reports ``"dropped"`` (would drop).
    """
    live = target.live
    if target.load == live:
        return "absent"
    if not inputs.live_database_exists:
        _log.info("previous_database_absent", database=live.name)
        return "none"
    if not removes_database(inputs.header.restore_mode):
        _log.info("previous_database_retained", database=live.name, reason="mode")
        return "retained"
    if not target.same_server:
        _log.info(
            "previous_database_retained", database=live.name, reason="different server"
        )
        return "retained"
    command = ports.adapters.restore_database_command
    journal.step_completed(_PHASE, STEP_LOSER_DROP_STARTED)
    if target.live_user is not None:
        user = target.live_user
        command.revoke_grants(live, user=user, confirm=user)
    dropped = command.drop_database(live, confirm=live.name)
    journal.step_completed(_PHASE, STEP_LOSER_DROPPED)
    _log.info("previous_database_dropped", database=live.name, performed=dropped)
    return "dropped" if dropped or not performed else "none"


def load_staged(
    inputs: RestoreInputs,
    journal: RunJournalCommand,
    *,
    ports: RestorePorts,
    workspace: Path,
) -> DatabaseRestoreReport:
    """``LOAD_DUMP``: ensure, load, check, cut over, then the loser.

    Raises :class:`StagingCheckFailedError` or :class:`ConfigCutoverError`
    (``FAILED``); database's refusal of a non-empty load target is
    ``DatabaseRefusedError`` (``REFUSED``). A header built for another load
    database or key set is a programming error (``ValueError``), raised
    before any call.

    FR-9: the returned report's ``restored`` is
    ``core/restore.py::database_restored`` of the load's result, the check's
    and the keys the cutover wrote; it is logged as ``database_restore_report``
    (INFO). Any failure is logged as ``database_restore_report`` at ERROR
    with ``restored: false`` and what is known — the ``ensure`` flags, the
    provisioning steps that completed, the error class — and re-raised; a
    failed load is never returned as a value.
    """
    target = _target(inputs)
    ensure: EnsureOutcome | None = None
    written: tuple[str, ...] | None = None
    try:
        _validate_name(target.load.name)
        keys = _check_header(inputs, target)
        _require_password_source(inputs, ports)
        adapters = ports.adapters
        command = adapters.restore_database_command
        ensure = command.ensure_database(target.load)
        if ensure.created_database:
            _log.info(
                "staging_database_created",
                staging_database=target.load.name,
                created_user=ensure.created_user,
                granted=ensure.granted,
                warnings=list(ensure.warnings),
            )
        try:
            adapters.site_files_command.extract(
                inputs.archive,
                workspace,
                inputs.view,
                roles=frozenset({MemberRole.DUMP}),
                payload=None if inputs.payload is None else Path(inputs.payload),
            )
            loaded = command.load_dump(target.load, workspace / DUMP_RELATIVE)
            checked = _post_load_check(
                inputs, target, loaded, ports=ports, journal=journal
            )
        except BaseException:
            _drop_staging(target, ensure, ports=ports, journal=journal)
            raise
        written = _cutover(inputs, target, keys, ensure, ports=ports, journal=journal)
        previous = _loser(
            inputs, target, ports=ports, journal=journal, performed=loaded.performed
        )
    except BaseException as exc:
        _report_failure(target, ensure, written, exc)
        raise
    report = DatabaseRestoreReport(
        live_database_before=target.live.name,
        live_database_after=target.load.name,
        ensure=ensure,
        loaded_bytes=loaded.bytes,
        load_duration_ms=loaded.duration_ms,
        previous_database=previous,
        restored=database_restored(
            loaded, check_passed=checked, cutover_written=written == keys
        ),
        performed=loaded.performed,
    )
    _log.info("database_restore_report", **database_report_fields(report))
    return report


def _report_failure(
    target: _Target,
    ensure: EnsureOutcome | None,
    written: tuple[str, ...] | None,
    exc: BaseException,
) -> None:
    """``database_restore_report`` at ERROR: what is known, never ``restored``.

    Fields are names, flags, codes and the error class — never the error's
    text, which may quote the dependency (AD-7). The cutover's progress is
    stated: the keys written (``cutover_written``) and, once every key
    landed, ``cutover_completed`` with the database the site now runs on —
    so a failure after the cutover (the loser's drop) does not read as "the
    restore did not happen".
    """
    fields: dict[str, object] = {
        "live_database_before": target.live.name,
        "load_database": target.load.name,
        "restored": False,
        "error_type": type(exc).__name__,
    }
    if ensure is not None:
        fields.update(
            created_database=ensure.created_database,
            created_user=ensure.created_user,
            password_set=ensure.password_set,
            granted=ensure.granted,
            warnings=list(ensure.warnings),
            performed=ensure.performed,
        )
    if written is not None:
        fields.update(
            cutover_written=list(written),
            cutover_completed=True,
            live_database_after=target.load.name,
        )
    elif isinstance(exc, ConfigCutoverError):
        fields.update(
            cutover_written=list(exc.written),
            cutover_pending=list(exc.pending),
            cutover_completed=False,
        )
    if isinstance(exc, ProvisioningFailedError):
        fields["completed"] = list(exc.completed)
    if isinstance(exc, DatabaseRefusedError):
        fields["findings"] = list(exc.findings)
    _log.error("database_restore_report", **fields)


# --------------------------------------------------------------------------
# Resolution: completing an interrupted cutover set
# --------------------------------------------------------------------------


def complete_cutover(record: JournalRecord, *, ports: RestorePorts) -> tuple[str, ...]:
    """Complete the cutover set of a Run stopped inside it; the keys written.

    Called by ``--resolve-interrupted-run`` (E003-S02-006) before
    ``RunJournalCommand.resolve``. Only when the Run stopped at a
    ``"cutover_started:*"`` or ``"cutover:*"`` step with keys pending: the
    keys are re-read through ``ConfigQuery.read_constants`` to learn what
    actually landed, and each pending key not already at its target is
    written from the header's non-secret targets; ``DB_PASSWORD`` from
    ``ports.cutover_credentials``, else ``CredentialsError`` naming it and
    nothing is written. Any other journal: nothing is written.
    """
    header, entries = record.header, record.entries
    phase = interrupted_phase(entries)
    step = last_step(entries, phase)
    if phase is not _PHASE or step is None:
        return ()
    if not step.startswith((STEP_CUTOVER, STEP_CUTOVER_STARTED)):
        return ()
    _, pending = cutover_progress(entries, header)
    if not pending:
        return ()
    credentials = ports.cutover_credentials
    if _PASSWORD in pending and credentials is None:
        raise CredentialsError(CUTOVER_PASSWORD_SOURCE, missing_key=_PASSWORD)
    config_file = Path(header.wp_config_path)
    adapters = ports.adapters
    current = adapters.config_query.read_constants(config_file, CUTOVER_KEYS)
    written: list[str] = []
    for key in pending:
        if key == _PASSWORD and credentials is not None:
            value: str | None = credentials()
        else:
            value = header.cutover_targets[key]
            if current.get(key) == value:
                continue
        try:
            adapters.config_command.set_constants(config_file, {key: value or ""})
        finally:
            value = None
        written.append(key)
    _log.info("cutover_completed", keys=written, wp_config_path=str(config_file))
    return tuple(written)
