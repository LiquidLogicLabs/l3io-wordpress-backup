"""Owner decisions 49 to 55, ADR-0027, AD-18 — the direct load and its rollback.

:func:`load_live` is the real ``load_database`` slot of
``adapters/restore_runner.py``'s ``execute_restore`` and :func:`check_live`
its ``check_database`` slot (the database part of ``PREFLIGHT``). The root
binds them with
``functools.partial`` over the Run's
:class:`~l3io.wp.backup.ports.restore.RestorePorts`; a Dry Run is the same
calls over ``dry_run_adapters(...)``, so this module has no branch on it.

**The direct load (ADR-0027, decisions 49 and 55).** The Dump loads straight
into the live database, as the WordPress account, after the Safety Snapshot:
there is no staging database and no ``DB_NAME`` cutover. ``load_live``
journals each step through the journal it is handed:

1. when the WordPress account cannot load the database as it stands (the
   database is absent, or the account lacks a right), ``ensure_database``
   as the administrator, the one use an administrator has (decision 55;
   ``"database_created"`` when it created the database);
2. ``"load_started"``, then ``load_dump(..., replace=True,
   prune_prefix=<the site's table prefix>)``: database drops every table and
   view carrying the prefix, then loads the Dump, so the database holds the
   Archive's tables and no others with that prefix (decision 53); tables
   without the prefix are never touched;
3. the post-load check, ``survey_target`` of the loaded database
   (:class:`LoadCheckFailedError` when it is not WordPress), then
   ``"loaded"``; a not-performed ``LoadResult`` (a Dry Run) surveys nothing;
4. a relocation's cutover (``--new-db-*``): one
   ``ConfigCommand.set_constants(config_file, {key: value})`` per key, each
   preceded by ``"cutover_started:<KEY>"`` and followed by
   ``"cutover:<KEY>"``. A restore that relocates nothing writes no key.

**The automatic rollback (decision 50).** Any failure inside ``LOAD_DUMP``
after ``"load_started"`` (the load, its check, or a cutover that wrote no
key) puts the live database back before the Run exits
(:func:`roll_back`): the Safety Snapshot's Dump is loaded the same way
(``"rollback_started"``, ``"rolled_back"``), or, when the database held
nothing before, the tables loaded are dropped (a database this Run created
is dropped). The Run's own error is then re-raised, and the failure is
orderly (``core/restore_journal.is_orderly_failure``). If the rollback
fails, or nothing can roll back (``--no-safety-snapshot`` over a database
with content), ``"rollback_failed"`` is journaled, the Run stays recorded as
interrupted, and :class:`RollbackFailedError` names both failures, the
snapshot and the recovery command. A SIGTERM (``RunTerminated``) or Ctrl-C
during the load is not rolled back: the Run stops at once, recorded
interrupted, and the undo text names the snapshot. A relocation that loads
another database leaves the live one untouched; a relocated database this
Run created is dropped again (``"load_target_dropped"``).

No SQL is issued and no config byte is written here: database and
``adapters/config_writer`` do both. ``l3io.wp.database`` is never imported
here: the load name is checked through
``adapters/database.database_name_is_valid`` (ADR-0015), so this module
imports without the ``database`` extra; it imports nothing from
``l3io.wp.config``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final, NoReturn

import structlog

from l3io.wp.backup.adapters.clock import SystemClock
from l3io.wp.backup.adapters.database import (
    ADMIN_SOURCE,
    blocked_dump_error,
    database_location_from_db_host,
    database_name_is_valid,
)
from l3io.wp.backup.adapters.journal import CorruptJournalError
from l3io.wp.backup.adapters.tar import (
    decrypt_payload,
    read_inner_manifest,
    verify_archive_manifest,
)
from l3io.wp.backup.core.restore import (
    CUTOVER_KEYS,
    NOT_ENSURED,
    DatabaseRestoreReport,
    RestoreInputs,
    database_report_fields,
    database_restored,
    load_database_name,
    planned_cutover_keys,
    planned_cutover_targets,
)
from l3io.wp.backup.core.restore_journal import (
    STEP_CONFIG_RESTORED,
    STEP_CUTOVER,
    STEP_CUTOVER_READBACK_FAILED,
    STEP_CUTOVER_STARTED,
    STEP_DATABASE_CREATED,
    STEP_LOAD_STARTED,
    STEP_LOAD_TARGET_DROPPED,
    STEP_LOADED,
    STEP_RELOCATION_ROLLED_BACK,
    STEP_ROLLBACK_FAILED,
    STEP_ROLLBACK_STARTED,
    STEP_ROLLED_BACK,
    cutover_progress,
    interrupted_phase,
    last_step,
    recovery_command,
)
from l3io.wp.backup.domain.archive_format import (
    DUMP_RELATIVE,
    MemberRole,
    check_inner_manifest,
)
from l3io.wp.backup.domain.database import (
    DatabaseLocation,
    EnsureOutcome,
    LoadResult,
    Readiness,
)
from l3io.wp.backup.domain.errors import (
    AdminCredentialsRequiredError,
    BackupError,
    ConfigWriteFailedError,
    CredentialsError,
    DatabaseRefusedError,
    PayloadDamagedError,
    ProvisioningFailedError,
    RefusedError,
    RunTerminated,
    VerificationFailedError,
)
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.domain.journal import (
    JOURNALED_CONSTANTS,
    JournalHeader,
    JournalRecord,
)
from l3io.wp.backup.domain.restore import RestorePhase
from l3io.wp.backup.domain.storage_target import StorageTarget
from l3io.wp.backup.ports.clock import Clock
from l3io.wp.backup.ports.config import WHOLE_FILE
from l3io.wp.backup.ports.crypto import PayloadCipher
from l3io.wp.backup.ports.journal import RunJournalCommand
from l3io.wp.backup.ports.restore import RestorePorts

__all__ = [
    "ADMINISTRATOR_RESOLVES",
    "CUTOVER_PASSWORD_SOURCE",
    "ConfigCutoverError",
    "CutoverTargetUnusableError",
    "InvalidLoadDatabaseNameError",
    "LoadCheckFailedError",
    "RollbackFailedError",
    "check_live",
    "complete_cutover",
    "load_live",
    "roll_back",
    "roll_back_relocation",
]

_log = structlog.get_logger(__name__)
#: The default ``Clock`` of :func:`load_live` when a caller passes none.
SYSTEM_CLOCK: Final = SystemClock()

#: ``check_load``'s blockers an administrator resolves with ``ensure``
#: (database's ``Code`` values): the database is absent, the account is
#: absent or cannot log in, or it lacks a right on the database (``ensure``
#: grants it ALL on the database). Owner decision 55: only these call for an
#: administrator.
ADMINISTRATOR_RESOLVES: Final[frozenset[str]] = frozenset(
    {"database_absent", "user_absent", "authentication_failed", "missing_privilege"}
)
#: The account a load check answers for when no ``DB_USER`` is known.
WORDPRESS_ACCOUNT: Final = "the WordPress account"
#: The label a missing relocated ``DB_PASSWORD`` source is named by.
CUTOVER_PASSWORD_SOURCE: Final = "relocated WordPress account"  # noqa: S105 - a label
_PASSWORD: Final = "DB_PASSWORD"  # noqa: S105 - a key name
_PHASE: Final = RestorePhase.LOAD_DUMP
_NS_PER_MS: Final = 1_000_000
#: Where the Safety Snapshot's Dump is extracted for a rollback, in the Run's
#: workspace.
_ROLLBACK_DIR: Final = "rollback"
_ROLLBACK_PAYLOAD: Final = "rollback-payload"


class LoadCheckFailedError(BackupError):
    """The loaded database is not a WordPress database (the post-load check)."""

    def __init__(
        self, database: str, *, is_wordpress: bool, schema_version: str | None
    ) -> None:
        self.database = database
        self.is_wordpress = is_wordpress
        self.schema_version = schema_version
        super().__init__(
            f"the loaded database {database!r} failed its check (is_wordpress="
            f"{is_wordpress}, schema_version={schema_version})"
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


def _failure_text(exc: BaseException) -> str:
    """An error's class, never its text, which may quote a dependency (AD-7)."""
    return type(exc).__name__


class RollbackFailedError(BackupError):
    """Decision 50: the load failed, and putting the database back failed too.

    Decision 72 too: ``SEARCH_REPLACE`` failed after a relocation, and putting
    the database or the original ``wp-config.php`` back failed.

    ``FAILED``. ``cause`` is the load's failure and ``rollback`` the
    rollback's (``None`` when nothing could roll back: no Safety Snapshot);
    ``database`` the live database, now partly loaded; ``snapshot`` the
    Safety Snapshot's path (``None`` without one) and ``recovery`` the
    commands that restore it. The Run stays recorded as interrupted. Names,
    paths, error classes and this package's own messages only.
    """

    def __init__(
        self,
        *,
        cause: BaseException,
        rollback: BaseException | None,
        database: str,
        snapshot: str | None,
        recovery: str,
    ) -> None:
        self.cause = cause
        self.rollback = rollback
        self.database = database
        self.snapshot = snapshot
        self.recovery = recovery
        failed = (
            "no Safety Snapshot was taken, so nothing could roll it back"
            if rollback is None
            else f"rolling it back from the Safety Snapshot failed too "
            f"({_failure_text(rollback)})"
        )
        what = (
            f"restore it from the Safety Snapshot {snapshot!r}: {recovery}"
            if snapshot is not None
            else "restore the database from another backup"
        )
        super().__init__(
            f"the restore into the database {database!r} failed "
            f"({_failure_text(cause)}); {failed}. The database is "
            f"partly restored and the run is recorded as interrupted; {what}"
        )


# --------------------------------------------------------------------------
# Where the Dump loads
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Target:
    live: DatabaseLocation
    load: DatabaseLocation
    #: The WordPress account the Dump loads as: the relocated user, else the
    #: live one (prior ``DB_USER``).
    load_user: str | None

    @property
    def loads_live(self) -> bool:
        """Owner decision 49: the Dump loads into the live database itself."""
        return self.load == self.live


def _target(inputs: RestoreInputs) -> _Target:
    site, relocation = inputs.site, inputs.relocation
    live = site.database_location()
    base = relocation.load_location or live
    prior = inputs.header.prior_constants.get("DB_USER")
    return _Target(
        live=live,
        load=base.with_name(load_database_name(site.db_name, relocation)),
        load_user=relocation.cutover_user or prior,
    )


def _validate_name(name: str) -> None:
    """Database's ``validate_database_name``, before the first call using ``name``."""
    if not database_name_is_valid(name):
        raise InvalidLoadDatabaseNameError(name)


def _require_password_source(inputs: RestoreInputs, ports: RestorePorts) -> None:
    keys = planned_cutover_keys(inputs.relocation)
    if _PASSWORD in keys and ports.cutover_credentials is None:
        raise CredentialsError(CUTOVER_PASSWORD_SOURCE, missing_key=_PASSWORD)


# --------------------------------------------------------------------------
# PREFLIGHT — queries only
# --------------------------------------------------------------------------


def _refuse(
    location: DatabaseLocation, blockers: tuple[str, ...], *, account: str
) -> None:
    """Refuse on ``blockers``, naming the database and ``account`` (never a secret)."""
    if not blockers:
        return
    error = blocked_dump_error(location, blockers, credential_source=account)
    if error is not None:
        raise error
    raise DatabaseRefusedError(
        findings=blockers, database=location.name, account=account
    )


def _readiness(
    inputs: RestoreInputs, ports: RestorePorts, target: _Target
) -> Readiness:
    return ports.adapters.restore_database_query.check_load(
        target.load,
        source=None,
        replace=True,
        prune_prefix=inputs.site.table_prefix,
    )


def _administrator_needed(readiness: Readiness) -> tuple[str, ...]:
    """The blockers only an administrator resolves (decision 55), in order."""
    return tuple(b for b in readiness.blockers if b in ADMINISTRATOR_RESOLVES)


def check_live(inputs: RestoreInputs, *, ports: RestorePorts) -> None:
    """The database part of ``PREFLIGHT``: database's checks, before any command.

    ``check_load`` as the WordPress account, with the load's own options
    (``replace``, ``prune_prefix``). Its blockers that only an administrator
    resolves (:data:`ADMINISTRATOR_RESOLVES`) call for ``check_ensure`` as
    the administrator; with none configured, ``AdminCredentialsRequiredError``
    names what is missing (owner decision 55). Any other blocker refuses.
    Nothing is changed, a Dry Run included.
    """
    target = _target(inputs)
    _validate_name(target.load.name)
    _require_password_source(inputs, ports)
    readiness = _readiness(inputs, ports, target)
    account = target.load_user or WORDPRESS_ACCOUNT
    needed = _administrator_needed(readiness)
    others = tuple(b for b in readiness.blockers if b not in ADMINISTRATOR_RESOLVES)
    _refuse(target.load, others, account=account)
    if not needed:
        _log.info(
            "database_load_ready",
            database=target.load.name,
            administrator_needed=False,
        )
        return
    try:
        ensure = ports.adapters.restore_database_query.check_ensure(target.load)
    except AdminCredentialsRequiredError as exc:
        raise AdminCredentialsRequiredError(
            exc.source,
            flags=exc.flags,
            missing=needed,
            database=target.load.name,
            account=account,
        ) from exc
    _refuse(target.load, ensure.blockers, account=ADMIN_SOURCE)
    _log.info(
        "database_load_ready",
        database=target.load.name,
        administrator_needed=True,
        missing=list(needed),
    )


# --------------------------------------------------------------------------
# LOAD_DUMP
# --------------------------------------------------------------------------


def _check_header(inputs: RestoreInputs, target: _Target) -> tuple[str, ...]:
    """The cutover keys; ``ValueError`` when the header was built for another load."""
    header = inputs.header
    keys = planned_cutover_keys(inputs.relocation)
    expected = (target.load.name, None, keys)
    found = (header.load_database, header.staging_database, header.cutover_keys)
    if expected != found:
        msg = (
            f"the journal header names load {found[0]!r}, staging {found[1]!r} and "
            f"keys {list(found[2])}; this Run loads {expected[0]!r}, no staging "
            f"database, keys {list(keys)}"
        )
        raise ValueError(msg)
    return keys


def _post_load_check(
    inputs: RestoreInputs,
    target: _Target,
    loaded: LoadResult,
    *,
    ports: RestorePorts,
) -> bool:
    """Structural: the loaded database must be WordPress with a schema version.

    Given a not-performed ``LoadResult`` (a Dry Run's ``NoopDatabase``) it
    surveys nothing, reports "would check" instead (ADR-0022) and returns
    ``False``: nothing was checked. ``True`` only for a check that passed.
    """
    name = target.load.name
    if not loaded.performed:
        _log.info("database_load_checked", database=name, performed=False)
        _log.info("would_check", database=name)
        return False
    state = ports.adapters.restore_database_query.survey_target(
        target.load,
        table_prefix=inputs.site.table_prefix,
        wordpress_user=target.load_user,
    )
    _log.info(
        "database_load_checked",
        database=name,
        is_wordpress=state.is_wordpress,
        schema_version=state.schema_version,
        performed=True,
    )
    if state.is_wordpress is not True or state.schema_version is None:
        raise LoadCheckFailedError(
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


def _read_back(
    ports: RestorePorts, config_file: Path, key: str, value: str
) -> bool | None:
    """Whether ``key`` now holds ``value``; ``None`` when it cannot be read.

    ADV-S02-04: after a write raised something other than
    ``ConfigWriteFailedError`` (whose contract is that the key did not land),
    the file itself says whether the one-key atomic write landed.
    """
    try:
        current = ports.adapters.config_query.read_constants(config_file, (key,))
    except RunTerminated:
        raise  # BL-E002-092
    except Exception as exc:  # unknown: the caller re-raises the write's error
        _log.error("cutover_read_back_failed", key=key, error_type=type(exc).__name__)
        return None
    return current.get(key) == value


def _cutover(
    inputs: RestoreInputs,
    target: _Target,
    keys: tuple[str, ...],
    *,
    ports: RestorePorts,
    journal: RunJournalCommand,
    clock: Clock,
) -> tuple[str, ...]:
    """A relocation's cutover: one atomic, verified ``set_constants`` per key.

    Its duration is read from ``clock`` (the Run's ``Clock`` port,
    BL-E003-100). Returns the keys written, in order. Each key's value is
    resolved before its ``"cutover_started:<KEY>"`` intent, so a value that
    cannot be had leaves no intent for a write that never began (ADV-S02-04).
    A write that raises is classified: a ``ConfigWriteFailedError`` did not
    land; any other error is read back — not at its target, it did not land,
    and is handled as a ``ConfigWriteFailedError``; at its target, it landed
    and the cutover goes on; unreadable, the error is re-raised and the
    intent stays the last step, because whether it landed is then unknown.
    """
    if not keys:
        return ()
    config_file = Path(inputs.relocation.config_file or inputs.config_path)
    command = ports.adapters.config_command
    started = clock.monotonic_ns()
    landed: list[str] = []
    for key in keys:
        value: str | None = _cutover_value(key, inputs, ports, target.load.name)
        journal.step_completed(_PHASE, STEP_CUTOVER_STARTED + key)
        try:
            command.set_constants(config_file, {key: value or ""})
        except RunTerminated:
            raise  # BL-E002-092: never read back and carried on past a SIGTERM
        except Exception as exc:
            arrived = (
                False
                if isinstance(exc, ConfigWriteFailedError)
                else _read_back(ports, config_file, key, value or "")
            )
            if arrived is None:
                raise
            if not arrived:
                if not landed:
                    journal.step_completed(_PHASE, STEP_CUTOVER_READBACK_FAILED)
                pending = tuple(k for k in keys if k not in landed)
                raise ConfigCutoverError(
                    str(config_file), key, written=tuple(landed), pending=pending
                ) from exc
            _log.warning(
                "cutover_write_error_landed", key=key, error_type=type(exc).__name__
            )
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
        phase=_PHASE.name,
        step="cutover",
        duration_ms=(clock.monotonic_ns() - started) / _NS_PER_MS,
        bytes=0,
        outcome="ok",
        exit_code=int(ExitCode.OK),
    )
    return tuple(landed)


def _ensure(
    inputs: RestoreInputs,
    target: _Target,
    *,
    ports: RestorePorts,
    journal: RunJournalCommand,
) -> EnsureOutcome | None:
    """Decision 55: the administrator's ``ensure``, only where it is needed.

    ``check_load`` is asked again here, as the WordPress account: the
    preflight's answer is not carried across phases (ADR-0022), and the
    administrator is used only when the account still cannot load.
    """
    needed = _administrator_needed(_readiness(inputs, ports, target))
    if not needed:
        return None
    ensure = ports.adapters.restore_database_command.ensure_database(target.load)
    if ensure.created_database:
        journal.step_completed(_PHASE, STEP_DATABASE_CREATED)
    _log.info(
        "database_ensured_for_load",
        database=target.load.name,
        missing=list(needed),
        created_database=ensure.created_database,
        created_user=ensure.created_user,
        granted=ensure.granted,
        warnings=list(ensure.warnings),
    )
    return ensure


def _extract_dump(
    inputs: RestoreInputs, *, ports: RestorePorts, workspace: Path
) -> Path:
    ports.adapters.site_files_command.extract(
        inputs.archive,
        workspace,
        inputs.view,
        roles=frozenset({MemberRole.DUMP}),
        payload=None if inputs.payload is None else Path(inputs.payload),
    )
    return workspace / DUMP_RELATIVE


def load_live(
    inputs: RestoreInputs,
    journal: RunJournalCommand,
    *,
    ports: RestorePorts,
    workspace: Path,
    cipher: PayloadCipher | None = None,
    clock: Clock = SYSTEM_CLOCK,
) -> DatabaseRestoreReport:
    """``LOAD_DUMP``: ensure if needed, load, check, a relocation's cutover.

    ``clock`` times the cutover: the Run's ``Clock`` (the restore root passes
    ``RunContext.clock``), the system clock when none is given. ``cipher``
    decrypts an encrypted Safety Snapshot for the rollback.

    Raises :class:`LoadCheckFailedError` or :class:`ConfigCutoverError`
    (``FAILED``), the load's own errors, or :class:`RollbackFailedError` when
    the rollback that follows one of them fails (:func:`roll_back`). A
    header built for another load database or key set is a programming error
    (``ValueError``), raised before any call.

    FR-9: the returned report's ``restored`` is
    ``core/restore.py::database_restored`` of the load's result, the check's
    and the keys the cutover wrote; it is logged as ``database_restore_report``
    (INFO). Any failure is logged as ``database_restore_report`` at ERROR
    with ``restored: false`` and what is known, and re-raised; a failed load
    is never returned as a value.
    """
    target = _target(inputs)
    ensure: EnsureOutcome | None = None
    written: tuple[str, ...] | None = None
    started = cutting = False
    try:
        _validate_name(target.load.name)
        keys = _check_header(inputs, target)
        _require_password_source(inputs, ports)
        ensure = _ensure(inputs, target, ports=ports, journal=journal)
        dump = _extract_dump(inputs, ports=ports, workspace=workspace)
        journal.step_completed(_PHASE, STEP_LOAD_STARTED)
        started = True
        loaded = ports.adapters.restore_database_command.load_dump(
            target.load,
            dump,
            replace=True,
            prune_prefix=inputs.site.table_prefix,
        )
        _log_dropped(target, loaded, inputs.site.table_prefix)
        checked = _post_load_check(inputs, target, loaded, ports=ports)
        journal.step_completed(_PHASE, STEP_LOADED)
        cutting = True
        written = _cutover(
            inputs, target, keys, ports=ports, journal=journal, clock=clock
        )
    except BaseException as exc:
        _report_failure(target, ensure, written, exc)
        if not started and not _interrupt(exc) and not target.loads_live:
            # Owner decision 73: a relocated database this Run created is
            # dropped again, whatever failed after ``ensure``.
            _drop_relocated(target, ensure, ports=ports, journal=journal)
        # A cutover error other than ``ConfigCutoverError`` leaves unknown
        # whether its key landed (ADV-S02-04): resolution decides, not a
        # rollback.
        ambiguous = cutting and not isinstance(exc, ConfigCutoverError)
        if started and not ambiguous:
            _undo_failed_load(
                inputs,
                journal,
                exc,
                ports=ports,
                workspace=workspace,
                cipher=cipher,
                ensure=ensure,
            )
        raise
    report = DatabaseRestoreReport(
        live_database_before=target.live.name,
        live_database_after=target.load.name,
        database=_where(target.load),
        ensure=NOT_ENSURED if ensure is None else ensure,
        administrator=ensure is not None,
        loaded_bytes=loaded.bytes,
        load_duration_ms=loaded.duration_ms,
        dropped_tables=loaded.dropped if loaded.performed else inputs.prefixed_tables,
        restored=database_restored(
            loaded, check_passed=checked, cutover_written=written == keys
        ),
        performed=loaded.performed,
    )
    _log.info("database_restore_report", **database_report_fields(report))
    return report


def _where(location: DatabaseLocation) -> str:
    return f"{location.host}:{location.port}/{location.name}"


def _log_dropped(target: _Target, loaded: LoadResult, prefix: str) -> None:
    """Decision 53: the tables the load removed first, by name, logged."""
    if not loaded.performed:
        return
    _log.info(
        "database_tables_dropped",
        database=target.load.name,
        table_prefix=prefix,
        count=len(loaded.dropped),
        tables=list(loaded.dropped),
    )


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
    landed, ``cutover_completed``.
    """
    fields: dict[str, object] = {
        "live_database_before": target.live.name,
        "load_database": target.load.name,
        "database": _where(target.load),
        "restored": False,
        "error_type": type(exc).__name__,
        "administrator": ensure is not None,
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
# The automatic rollback (owner decision 50)
# --------------------------------------------------------------------------


def _interrupt(exc: BaseException) -> bool:
    """A SIGTERM or Ctrl-C: the Run stops now, and is not rolled back."""
    return isinstance(exc, RunTerminated | KeyboardInterrupt) or not isinstance(
        exc, Exception
    )


def _undo_failed_load(
    inputs: RestoreInputs,
    journal: RunJournalCommand,
    exc: BaseException,
    *,
    ports: RestorePorts,
    workspace: Path,
    cipher: PayloadCipher | None,
    ensure: EnsureOutcome | None,
) -> None:
    """After ``"load_started"``: roll back, or drop a relocated database made here.

    A cutover that wrote a key is left for ``--resolve-interrupted-run``
    (its keys point the site at the loaded database). An interrupt is not
    rolled back (:func:`_interrupt`). Raises :class:`RollbackFailedError`
    from ``exc`` when the rollback fails; returns when the database is as it
    was, so the caller re-raises ``exc``.
    """
    if _interrupt(exc):
        return
    if isinstance(exc, ConfigCutoverError) and exc.written:
        return
    target = _target(inputs)
    if not target.loads_live:
        _drop_relocated(target, ensure, ports=ports, journal=journal)
        return
    roll_back(
        inputs,
        journal,
        exc,
        ports=ports,
        workspace=workspace,
        cipher=cipher,
        created=ensure is not None and ensure.created_database,
    )


def _drop_relocated(
    target: _Target,
    ensure: EnsureOutcome | None,
    *,
    ports: RestorePorts,
    journal: RunJournalCommand,
) -> None:
    """A relocated database this Run created is dropped again; the live is intact.

    One that existed before is left as the load left it (the undo text says
    so). A failed drop is logged, and the journal stays blocking; the caller
    re-raises the original error.
    """
    if ensure is None or not ensure.created_database:
        return
    name = target.load.name
    try:
        ports.adapters.restore_database_command.drop_database(target.load, confirm=name)
        journal.step_completed(_PHASE, STEP_LOAD_TARGET_DROPPED)
    except RunTerminated:
        raise  # BL-E002-092
    except Exception as exc:  # the original error is re-raised by the caller
        _log.error(
            "load_database_drop_failed", database=name, error_type=type(exc).__name__
        )
        return
    _log.warning("load_database_dropped", database=name)


def _snapshot_dump(
    snapshot: str,
    *,
    ports: RestorePorts,
    workspace: Path,
    cipher: PayloadCipher | None,
) -> Path:
    """The Safety Snapshot's Dump, verified again and extracted into the workspace.

    The snapshot was verified after it was published (FR-8); it is verified
    again here, as ``VERIFY`` verifies an Archive, and an encrypted one's
    inner Manifest must equal the outer one (ADR-0024 amendment) before its
    Dump is read.
    """
    archive = StorageTarget(scheme="file", path=PurePosixPath(snapshot))
    view, result, text = verify_archive_manifest(Path(snapshot), phase_key=_PHASE)
    if view is None or not result.passed:
        raise VerificationFailedError(result, snapshot)
    payload: Path | None = None
    if view.encrypted:
        if cipher is None or text is None:
            msg = "the Safety Snapshot is encrypted and no passphrase is bound"
            raise ValueError(msg)
        payload = decrypt_payload(
            archive, view, cipher=cipher, destination=workspace / _ROLLBACK_PAYLOAD
        )
        field = check_inner_manifest(
            text, read_inner_manifest(payload, phase_key=_PHASE)
        )
        if field is not None:
            raise PayloadDamagedError(field=field)
    destination = workspace / _ROLLBACK_DIR
    destination.mkdir(mode=0o700, exist_ok=True)
    ports.adapters.site_files_command.extract(
        archive, destination, view, roles=frozenset({MemberRole.DUMP}), payload=payload
    )
    return destination / DUMP_RELATIVE


def roll_back(
    inputs: RestoreInputs,
    journal: RunJournalCommand,
    cause: BaseException,
    *,
    ports: RestorePorts,
    workspace: Path,
    cipher: PayloadCipher | None,
    created: bool = False,
) -> None:
    """Put the live database back as it was before the load (decision 50).

    ``inputs.rollback`` says how (``core/restore.database_rollback``):
    ``"snapshot"`` loads the Safety Snapshot's Dump the same way the load
    did (``replace``, ``prune_prefix``); ``"empty"`` drops the tables the
    load created, or the database itself when this Run created it
    (``created``, as the administrator). ``"not-live"`` changes nothing.
    ``"none"``, or any failure of the rollback, journals
    ``"rollback_failed"`` and raises :class:`RollbackFailedError` naming
    ``cause``, the rollback's failure, the snapshot and the recovery
    command. Success journals ``"rolled_back"`` and logs WARNING
    ``database_rolled_back``; the caller then re-raises ``cause``.
    """
    kind = inputs.rollback
    if kind == "not-live":
        return
    target = _target(inputs)
    header = inputs.header
    prefix = inputs.site.table_prefix
    command = ports.adapters.restore_database_command
    _log.warning(
        "database_rollback_started",
        database=target.load.name,
        rollback=kind,
        snapshot=header.snapshot_path,
        cause=type(cause).__name__,
    )
    snapshot = header.snapshot_path
    if kind == "none" or (kind == "snapshot" and snapshot is None):
        _rollback_failed(inputs, journal, cause, None, target=target)
    try:
        journal.step_completed(_PHASE, STEP_ROLLBACK_STARTED)
        if kind == "snapshot" and snapshot is not None:
            dump = _snapshot_dump(
                snapshot, ports=ports, workspace=workspace, cipher=cipher
            )
            command.load_dump(target.load, dump, replace=True, prune_prefix=prefix)
        elif created:
            command.drop_database(target.load, confirm=target.load.name)
        else:
            command.drop_tables(target.load, prefix=prefix)
    except RunTerminated:
        raise
    except Exception as exc:  # named in RollbackFailedError, with the cause
        _rollback_failed(inputs, journal, cause, exc, target=target)
    journal.step_completed(_PHASE, STEP_ROLLED_BACK)
    _log.warning(
        "database_rolled_back",
        database=target.load.name,
        rollback=kind,
        snapshot=header.snapshot_path,
        cause=type(cause).__name__,
    )


def roll_back_relocation(
    inputs: RestoreInputs,
    journal: RunJournalCommand,
    cause: BaseException,
    created: bool,
    config_file: Path | None,
    *,
    ports: RestorePorts,
    workspace: Path,
    cipher: PayloadCipher | None,
) -> None:
    """Owner decision 72: a failed ``SEARCH_REPLACE`` puts the site back.

    The executor's ``undo_relocation`` slot, called when the URL rewrite
    fails after ``LOAD_DUMP`` (an interrupt is never rolled back). The
    database first: the live one is rolled back as a failed load is
    (:func:`roll_back`: the Safety Snapshot, or emptied when it was empty);
    a relocated one this Run ``created`` is dropped again, and one that
    existed (empty, decision 73) has the tables the load created dropped.
    Then ``config_file``, the original ``wp-config.php``, is replaced by its
    saved copy (the journal header's ``saved_config``), undoing
    ``REWRITE_CONFIG`` and the cutover at once; ``None`` when the target had
    none to put back. Journals ``"config_restored"`` and
    ``"relocation_rolled_back"`` (orderly unless files were extracted) and
    logs WARNING ``relocation_rolled_back``. Any failure journals
    ``"rollback_failed"`` and raises :class:`RollbackFailedError` naming
    ``cause`` (decision 50); the caller re-raises ``cause`` otherwise.
    """
    target = _target(inputs)
    saved = inputs.header.saved_config
    _log.warning(
        "relocation_rollback_started",
        database=target.load.name,
        wp_config_path=None if config_file is None else str(config_file),
        saved_config=saved,
        cause=type(cause).__name__,
    )
    if target.loads_live:
        roll_back(
            inputs,
            journal,
            cause,
            ports=ports,
            workspace=workspace,
            cipher=cipher,
            created=created,
        )
    else:
        _empty_relocated(
            inputs, journal, cause, target=target, ports=ports, created=created
        )
    if config_file is not None:
        try:
            _put_back_config(config_file, saved, ports=ports)
            journal.step_completed(RestorePhase.SEARCH_REPLACE, STEP_CONFIG_RESTORED)
        except RunTerminated:
            raise
        except Exception as exc:  # named in RollbackFailedError, with the cause
            _rollback_failed(inputs, journal, cause, exc, target=target)
    journal.step_completed(RestorePhase.SEARCH_REPLACE, STEP_RELOCATION_ROLLED_BACK)
    _log.warning(
        "relocation_rolled_back",
        database=target.load.name,
        wp_config_path=None if config_file is None else str(config_file),
        saved_config=saved,
        cause=type(cause).__name__,
    )


def _put_back_config(
    config_file: Path, saved: str | None, *, ports: RestorePorts
) -> None:
    """The saved copy's bytes over ``config_file``, atomically, nothing carried."""
    if saved is None:
        reason = "no saved copy of wp-config.php to put back"
        raise ConfigWriteFailedError(config_file, WHOLE_FILE, reason)
    text = Path(saved).read_bytes().decode("utf-8")
    ports.adapters.config_command.replace_file(config_file, text, keep=())


def _empty_relocated(
    inputs: RestoreInputs,
    journal: RunJournalCommand,
    cause: BaseException,
    *,
    target: _Target,
    ports: RestorePorts,
    created: bool,
) -> None:
    """The relocated database as it was: dropped if made here, else emptied."""
    command = ports.adapters.restore_database_command
    try:
        journal.step_completed(_PHASE, STEP_ROLLBACK_STARTED)
        if created:
            command.drop_database(target.load, confirm=target.load.name)
            journal.step_completed(_PHASE, STEP_LOAD_TARGET_DROPPED)
        else:
            command.drop_tables(target.load, prefix=inputs.site.table_prefix)
    except RunTerminated:
        raise
    except Exception as exc:  # named in RollbackFailedError, with the cause
        _rollback_failed(inputs, journal, cause, exc, target=target)
    journal.step_completed(_PHASE, STEP_ROLLED_BACK)


def _rollback_failed(
    inputs: RestoreInputs,
    journal: RunJournalCommand,
    cause: BaseException,
    failure: BaseException | None,
    *,
    target: _Target,
) -> NoReturn:
    header = inputs.header
    try:
        journal.step_completed(_PHASE, STEP_ROLLBACK_FAILED)
    except RunTerminated:
        raise
    except Exception as exc:  # the journal stays at rollback_started: blocking
        _log.error("rollback_failed_not_journaled", error_type=type(exc).__name__)
    error = RollbackFailedError(
        cause=cause,
        rollback=failure,
        database=_where(target.load),
        snapshot=header.snapshot_path,
        recovery=recovery_command(header),
    )
    _log.error(
        "database_rollback_failed",
        database=target.load.name,
        cause=type(cause).__name__,
        rollback_error=None if failure is None else type(failure).__name__,
        snapshot=header.snapshot_path,
        recovery=error.recovery,
    )
    raise error from cause


# --------------------------------------------------------------------------
# Resolution: completing an interrupted cutover set
# --------------------------------------------------------------------------


class CutoverTargetUnusableError(RefusedError):
    """ADV-S02-05: the database an interrupted cutover pointed at is unusable."""

    def __init__(self, database: str, *, run_id: str, exists: bool) -> None:
        self.database = database
        self.run_id = run_id
        self.exists = exists
        state = "is not a WordPress database" if exists else "no longer exists"
        super().__init__(
            f"the interrupted run {run_id} was switching wp-config.php to "
            f"{database!r}, which {state}; the cutover was not completed and "
            "nothing was written. Set the keys already written back to the "
            "journal's prior_constants, or restore from the Safety Snapshot, "
            f"then run --resolve-interrupted-run {run_id} again"
        )


def _cutover_target(header: JournalHeader, key: str, *, journal_path: str) -> str:
    """The header's target for a non-secret ``key``; a missing one is corrupt."""
    value = header.cutover_targets.get(key)
    if value is None:
        raise CorruptJournalError(
            journal_path, f"its header records no cutover target for {key}"
        )
    return value


def _load_location(
    header: JournalHeader, current: Mapping[str, object]
) -> DatabaseLocation | None:
    """The server and name the interrupted cutover points ``wp-config.php`` at.

    ``DB_HOST`` is the header's target when the cutover moves it, else the
    one ``wp-config.php`` holds now; ``None`` when neither is known.
    """
    host = header.cutover_targets.get("DB_HOST", current.get("DB_HOST"))
    if not isinstance(host, str) or not host:
        return None
    return database_location_from_db_host(host, header.load_database)


def _back_at_prior(header: JournalHeader, current: Mapping[str, object]) -> bool:
    """Whether every journaled cutover key holds its pre-Run value again."""
    return all(
        current.get(key) == header.prior_constants.get(key)
        for key in header.cutover_keys
        if key in JOURNALED_CONSTANTS
    )


def _require_load_database(
    header: JournalHeader,
    current: Mapping[str, object],
    *,
    ports: RestorePorts,
    table_prefix: str,
) -> bool:
    """Survey the load database before any write; ``False``: write nothing.

    A load database that still exists and passes the post-load check:
    ``True``. Otherwise, when ``wp-config.php`` is back at its pre-Run values
    (no key landed, or the operator set them back), there is nothing to roll
    forward: ``False``, logged ``cutover_abandoned``. Otherwise
    :class:`CutoverTargetUnusableError`.
    """
    location = _load_location(header, current)
    state = (
        None
        if location is None
        else ports.adapters.restore_database_query.survey_target(
            location,
            table_prefix=table_prefix,
            wordpress_user=header.cutover_targets.get(
                "DB_USER", header.prior_constants.get("DB_USER")
            ),
        )
    )
    usable = (
        state is not None
        and state.database_exists
        and state.is_wordpress is True
        and state.schema_version is not None
    )
    if usable:
        return True
    exists = state is not None and state.database_exists
    if _back_at_prior(header, current):
        _log.warning(
            "cutover_abandoned",
            load_database=header.load_database,
            database_exists=exists,
            reason="wp-config.php holds its pre-run values",
        )
        return False
    raise CutoverTargetUnusableError(
        header.load_database, run_id=header.run_id, exists=exists
    )


def complete_cutover(
    record: JournalRecord,
    *,
    ports: RestorePorts,
    table_prefix: str,
    journal_path: str,
) -> tuple[str, ...]:
    """Complete the cutover set of a Run stopped inside it; the keys written.

    Called by ``--resolve-interrupted-run`` (E003-S02-006) before
    ``RunJournalCommand.resolve``. Only when the Run stopped at a
    ``"cutover_started:*"`` or ``"cutover:*"`` step with keys pending: the
    keys are re-read through ``ConfigQuery.read_constants`` to learn what
    actually landed, and each pending key not already at its target is
    written from the header's non-secret targets; ``DB_PASSWORD`` from
    ``ports.cutover_credentials``, else ``CredentialsError`` naming it and
    nothing is written. Any other journal: nothing is written (a direct
    load's interruption is resolved by acknowledging it; its undo text names
    the Safety Snapshot and the recovery command).

    ADV-S02-05: before the first write the load database is surveyed
    (``table_prefix`` is the Site's); one that is gone or not WordPress is
    never written to (:func:`_require_load_database`). A header with no
    target for a pending key is :class:`CorruptJournalError` naming
    ``journal_path``, raised before any write.
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
    targets = {
        key: _cutover_target(header, key, journal_path=journal_path)
        for key in pending
        if key != _PASSWORD
    }
    config_file = Path(header.wp_config_path)
    adapters = ports.adapters
    current = adapters.config_query.read_constants(config_file, CUTOVER_KEYS)
    if not _require_load_database(
        header, current, ports=ports, table_prefix=table_prefix
    ):
        return ()
    written: list[str] = []
    for key in pending:
        if key == _PASSWORD and credentials is not None:
            value: str | None = credentials()
        else:
            value = targets[key]
            if current.get(key) == value:
                continue
        try:
            adapters.config_command.set_constants(config_file, {key: value or ""})
        finally:
            value = None
        written.append(key)
    _log.info("cutover_completed", keys=written, wp_config_path=str(config_file))
    return tuple(written)
