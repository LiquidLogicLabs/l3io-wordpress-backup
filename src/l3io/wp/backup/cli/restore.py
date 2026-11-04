"""``wpbackup2 restore`` — the flags' translation into the restore vocabulary.

The restore driver is Epic 3's (E003-S02-006 binds it and removes the
subcommand's ``NOT_YET_IMPLEMENTED`` entry). What exists now is the part the
root needs before any Run opens: the flags read into AD-25's
``(RestoreMode, OverrideSet)``, ``SnapshotPolicy`` and ``ConfigPolicy``, and
the parse-time usage checks, so a rejected combination exits ``USAGE`` with
the table's own message whether or not the driver has landed.

``--mode`` is translated through ``cli/scope.py``'s ``CLI_MODE_TO_SCOPE``, the
one place the CLI's ``db`` becomes ``database``.

FR-9's database report (E003-S02-004) is rendered here for the root to print:
:func:`database_report_lines` (human), :func:`database_report_json` (one JSON
object, for ``--log-format json``), :func:`emit_database_report` choosing
between them, and :func:`require_database_restored`, the exit rule — a real
Run whose plan loads the Dump exits ``OK`` only for a ``restored`` report. It
is the only one of these to name the Dry Run (AD-4: this is a root).
"""

from __future__ import annotations

import json

from l3io.wp.backup.cli.credentials import admin_spec, wordpress_spec
from l3io.wp.backup.cli.logging import configured_format
from l3io.wp.backup.cli.output import emit_result
from l3io.wp.backup.cli.scope import backup_scope_from_cli
from l3io.wp.backup.core.restore import (
    DatabaseRestoreReport,
    ExecutionReport,
    RestorePlan,
    database_report_fields,
)
from l3io.wp.backup.domain.errors import BackupError
from l3io.wp.backup.domain.flag_map import (
    FlagCombination,
    combination,
    resolve_flags,
)
from l3io.wp.backup.domain.restore import (
    ConfigPolicy,
    OverrideSet,
    RestoreMode,
    RestorePhase,
    SnapshotPolicy,
)

__all__ = [
    "DatabaseNotRestoredError",
    "check_restore_arguments",
    "config_policy_from_cli",
    "database_report_json",
    "database_report_lines",
    "emit_database_report",
    "flag_combination",
    "require_database_restored",
    "restore_mode_from_cli",
    "snapshot_policy_from_cli",
]

#: How a database warning code reads in the report; others print as the code.
_WARNING_TEXT = {
    "password_unverified": (
        "the WordPress account's host does not match this machine, so its "
        "password was not verified by logging in and an existing password was "
        "left unchanged"
    ),
}
_PREVIOUS_TEXT = {
    "dropped": "dropped",
    "retained": "retained, not removed",
    "none": "none (no live database existed)",
    "absent": "none (loaded in place)",
}


def flag_combination(
    *, mode: str, cleanfirst: bool, drop_database: bool, force: bool
) -> FlagCombination:
    """The AD-25 table key for the restore flags' parsed values."""
    return combination(
        mode=backup_scope_from_cli(mode),
        cleanfirst=cleanfirst,
        drop_database=drop_database,
        force=force,
    )


def restore_mode_from_cli(
    *, mode: str, cleanfirst: bool, drop_database: bool, force: bool
) -> tuple[RestoreMode, OverrideSet]:
    """The Mode and waivers the flags express; raises ``USAGE`` otherwise."""
    return resolve_flags(
        flag_combination(
            mode=mode, cleanfirst=cleanfirst, drop_database=drop_database, force=force
        )
    )


def snapshot_policy_from_cli(*, no_safety_snapshot: bool) -> SnapshotPolicy:
    """``--no-safety-snapshot`` is the explicit opt-out; the default takes one."""
    return SnapshotPolicy.SKIP if no_safety_snapshot else SnapshotPolicy.TAKE


def config_policy_from_cli(*, overwrite_wp_config: bool) -> ConfigPolicy:
    """``--overwrite-wp-config`` may replace the target's ``wp-config.php``."""
    return ConfigPolicy.OVERWRITE if overwrite_wp_config else ConfigPolicy.KEEP


def check_restore_arguments(
    *,
    mode: str,
    cleanfirst: bool,
    drop_database: bool,
    force: bool,
    admin_username: str | None,
    admin_password: str | None,
    admin_aws_secret_id: str | None,
    admin_aws_region: str | None,
    db_aws_secret_id: str | None,
    db_aws_region: str | None,
) -> None:
    """Raise ``UsageError`` for restore arguments no Run could act on.

    The flag combination must express a Restore Mode, and each credential
    flag pair must be whole. Nothing outside the arguments is read.
    """
    restore_mode_from_cli(
        mode=mode, cleanfirst=cleanfirst, drop_database=drop_database, force=force
    )
    admin_spec(
        username=admin_username,
        password=admin_password,
        aws_secret_id=admin_aws_secret_id,
        aws_region=admin_aws_region,
    )
    wordpress_spec(aws_secret_id=db_aws_secret_id, aws_region=db_aws_region)


# --------------------------------------------------------------------------
# FR-9: the database report, and the exit rule over it
# --------------------------------------------------------------------------


class DatabaseNotRestoredError(BackupError):
    """A real Run's plan loaded the Dump and the report does not say restored.

    ``FAILED``. Every database failure raises where it happens; this is the
    root's last check that "done" is never printed for a database not loaded.
    """

    def __init__(self, database: str | None) -> None:
        self.database = database
        named = "no database report" if database is None else repr(database)
        super().__init__(f"the database was not restored ({named})")


def _flag(done: bool, yes: str, no: str) -> str:
    return yes if done else no


def database_report_lines(report: DatabaseRestoreReport) -> tuple[str, ...]:
    """The report as human lines; a Dry Run's says "would", never "restored"."""
    after, before = report.live_database_after, report.live_database_before
    if not report.performed:
        would = [
            f"database: would ensure {after!r} (database, WordPress user, password, "
            "grant)",
            f"database: would load the Dump into {after!r}",
            f"database: would switch wp-config.php from {before!r} to {after!r}",
        ]
        if report.previous_database == "dropped":
            would.append(f"database: would drop the previous database {before!r}")
        return (*would, "database: not restored (Dry Run)")
    ensure = report.ensure
    lines = [
        f"database: the site now runs on {after!r} (was {before!r})",
        "database: "
        + _flag(ensure.created_database, "created", "already existed")
        + f" {after!r}",
        "database: WordPress user "
        + _flag(ensure.created_user, "created", "already existed"),
        "database: password " + _flag(ensure.password_set, "set", "unchanged"),
        "database: access " + _flag(ensure.granted, "granted", "already granted"),
        f"database: loaded {report.loaded_bytes} bytes in "
        f"{report.load_duration_ms:.0f} ms",
        f"database: previous database {before!r}: "
        + _PREVIOUS_TEXT[report.previous_database],
    ]
    lines.extend(
        f"database: warning {code}: {_WARNING_TEXT.get(code, code)}"
        for code in ensure.warnings
    )
    lines.append(_flag(report.restored, "database: restored", "database: NOT restored"))
    return tuple(lines)


def database_report_json(report: DatabaseRestoreReport) -> str:
    """The report as one JSON object (``--log-format json``)."""
    return json.dumps({"database_report": database_report_fields(report)})


def emit_database_report(report: DatabaseRestoreReport) -> None:
    """Print the report on stdout in the configured format."""
    if configured_format() == "json":
        emit_result(database_report_json(report))
        return
    emit_result("\n".join(database_report_lines(report)))


def require_database_restored(
    plan: RestorePlan, outcome: ExecutionReport, *, dry_run: bool
) -> None:
    """FR-9's exit rule; raises :class:`DatabaseNotRestoredError` (``FAILED``).

    A plan with ``LOAD_DUMP`` exits ``OK`` in a real Run only when the report
    says ``restored``; a Dry Run that completed its plan exits ``OK`` with a
    not-performed report (ADR-0022). A plan without ``LOAD_DUMP`` must carry
    no report at all.
    """
    loads = RestorePhase.LOAD_DUMP in plan.phases
    database = outcome.database
    if not loads:
        if database is not None:
            msg = "a plan without LOAD_DUMP returned a database report"
            raise ValueError(msg)
        return
    if dry_run and database is not None and not database.performed:
        return
    if database is None or not database.restored or not database.performed:
        raise DatabaseNotRestoredError(
            None if database is None else database.live_database_after
        )
