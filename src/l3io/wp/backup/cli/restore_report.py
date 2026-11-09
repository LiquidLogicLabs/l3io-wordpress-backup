"""FR-9, UX-S02-04 — what a restore prints, and FR-9's exit rule.

Split from ``cli/restore.py`` (BL-E003-109). The database report
(:func:`database_report_lines`, :func:`require_database_restored`), the one
``--log-format json`` object (:func:`restore_summary`), the human lines the
Run ends with (:func:`completion_line`, the skipped-link lines of a Dry Run)
and their printer. Built from a ``RestoreOutcome``
(``cli/restore_request.py``); nothing here runs a phase.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import PurePath
from types import MappingProxyType
from typing import Final, Protocol

import structlog

from l3io.wp.backup.cli.logging import configured_format
from l3io.wp.backup.cli.output import (
    emit_result,
)
from l3io.wp.backup.cli.restore_request import RestoreOutcome
from l3io.wp.backup.core.restore import (
    DatabaseRestoreReport,
    database_report_fields,
)
from l3io.wp.backup.domain.archive_format import (
    SkippedLink,
)
from l3io.wp.backup.domain.errors import (
    BackupError,
)
from l3io.wp.backup.domain.restore import (
    RestorePhase,
)
from l3io.wp.backup.domain.search_replace import SearchReplaceReport

__all__ = [
    "LINKS_AT_EXTRACTION",
    "DatabaseNotRestoredError",
    "completion_line",
    "database_report_json",
    "database_report_lines",
    "emit_database_report",
    "planned_link_lines",
    "print_kept_snapshot",
    "print_outcome",
    "require_database_restored",
    "restore_summary",
    "search_replace_fields",
    "search_replace_line",
]

_log = structlog.get_logger(__name__)

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


#: Why a performed load is not reported restored: one of the three FR-9 facts
#: (load, post-load check, cutover) did not hold.
_NOT_RESTORED_REASON: Final = (
    "the load, its post-load check or the cutover did not complete"
)


def database_report_lines(report: DatabaseRestoreReport) -> tuple[str, ...]:
    """The report as human lines; a Dry Run's says "would", never "restored".

    The verdict comes first (BL-E003-106, owner decision 2026-11-07):
    ``database restored`` or ``database NOT restored: <reason>``, then the
    details. The JSON form (:func:`database_report_json`) is unchanged.
    """
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
        return ("database NOT restored: Dry Run", *would)
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
    verdict = _flag(
        report.restored,
        "database restored",
        f"database NOT restored: {_NOT_RESTORED_REASON}",
    )
    return (verdict, *lines)


def database_report_json(report: DatabaseRestoreReport) -> str:
    """The report as one JSON object (``--log-format json``)."""
    return json.dumps({"database_report": database_report_fields(report)})


def emit_database_report(report: DatabaseRestoreReport) -> None:
    """Print the report on stdout in the configured format."""
    if configured_format() == "json":
        emit_result(database_report_json(report))
        return
    emit_result("\n".join(database_report_lines(report)))


class _Ran(Protocol):
    """What the exit rule reads: the phases that ran and the database report."""

    @property
    def phases(self) -> tuple[RestorePhase, ...]: ...

    @property
    def database(self) -> DatabaseRestoreReport | None: ...


def require_database_restored(outcome: _Ran, *, dry_run: bool) -> None:
    """FR-9's exit rule; raises :class:`DatabaseNotRestoredError` (``FAILED``).

    ``outcome`` is a :class:`RestoreOutcome` (or an ``ExecutionReport``):
    with ``LOAD_DUMP`` among its phases it exits ``OK`` in a real Run only
    when the report says ``restored``; a Dry Run that completed its plan exits
    ``OK`` with a not-performed report (ADR-0022). Without ``LOAD_DUMP`` it
    must carry no report at all.
    """
    loads = RestorePhase.LOAD_DUMP in outcome.phases
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


# --------------------------------------------------------------------------
# run_restore — the CLI root
# --------------------------------------------------------------------------


def _status(outcome: RestoreOutcome, *, completed: bool) -> str:
    if not completed:
        return "failed"
    if outcome.resolved_run_id is not None:
        return "would_resolve" if outcome.dry_run else "resolved"
    return "planned" if outcome.dry_run else "completed"


def restore_summary(
    outcome: RestoreOutcome, *, archive: str, site_root: str, completed: bool
) -> dict[str, object]:
    """UX-S02-04: everything a Run prints, as the one ``--log-format json`` object.

    ``status`` is ``completed`` (a real Run), ``planned`` (a Dry Run),
    ``resolved``/``would_resolve`` (``--resolve-interrupted-run``) or
    ``failed`` (the report does not call the database restored); ``actions``
    is the Dry Run's ordered plan, empty for a real Run; ``notes`` what the
    Run chose without asking (owner decisions 64 and 65).
    """
    database = outcome.database
    search = outcome.search_replace
    return {
        "status": _status(outcome, completed=completed),
        "run_id": outcome.run_id,
        "dry_run": outcome.dry_run,
        "restore_mode": outcome.mode.value,
        "archive": archive,
        "site_root": site_root,
        "resolved_run_id": outcome.resolved_run_id,
        "notes": list(outcome.notes),
        "actions": [a.text for a in outcome.actions] if outcome.dry_run else [],
        "skipped_links": (
            None
            if outcome.skipped_links is None
            else [
                {
                    "path": str(link.path),
                    "link_target": str(link.link_target),
                    "reason": link.reason,
                }
                for link in outcome.skipped_links
            ]
        ),
        "database_report": (
            None if database is None else database_report_fields(database)
        ),
        "search_replace": (None if search is None else search_replace_fields(search)),
        "snapshot_path": (
            None if outcome.snapshot_path is None else str(outcome.snapshot_path)
        ),
        "snapshot_encrypted": outcome.snapshot_encrypted,
        "wp_config_kept": outcome.wp_config_kept,
    }


def search_replace_fields(report: SearchReplaceReport) -> dict[str, object]:
    """FR-12: the content URL rewrite in the JSON report (URLs are not secrets).

    ``replacements``, ``tables`` and ``remaining`` are ``None`` in a Dry Run:
    unknown until the Dump is loaded, never a guessed number.
    """
    return {
        "replacements": report.replacements,
        "tables": None if report.tables is None else list(report.tables),
        "pairs": [{"old": p.old, "new": p.new} for p in report.pairs],
        "remaining": report.remaining,
        "merged": report.merged,
    }


def search_replace_line(report: SearchReplaceReport) -> str:
    """FR-12: ``content URLs: <n> replacements in <k> tables (<old> → <new>)``.

    A Dry Run's report has no counts: its line names the planned pairs and says
    the counts are determined after ``LOAD_DUMP``.
    """
    pairs = "; ".join(f"{p.old} → {p.new}" for p in report.pairs) or "none"
    if report.replacements is None or report.tables is None:
        return f"content URLs: planned ({pairs}); counts determined after LOAD_DUMP"
    return (
        f"content URLs: {report.replacements} replacements in "
        f"{len(report.tables)} tables ({pairs})"
    )


#: A Dry Run's words for :attr:`SkippedLink.reason`.
_LINK_REASONS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "absolute": "its target is absolute",
        "outside_destination": "its target resolves outside the Site Root",
    }
)
#: A Dry Run of an Archive that records no link targets (v0).
LINKS_AT_EXTRACTION: Final = (
    "symlinks: this Archive (v0) records no link targets; any the Run skips "
    "are reported at extraction"
)


def planned_link_lines(links: tuple[SkippedLink, ...] | None) -> tuple[str, ...]:
    """Owner decision 41: the Dry Run's lines for the links the Run would skip.

    One line per link, with its reason, or :data:`LINKS_AT_EXTRACTION` when
    the Archive records no link targets (``None``). The rule is lexical; a
    directory on the target that is already a symlink is seen only at
    extraction (``docs/restore.md``, Symlinks).
    """
    if links is None:
        return (LINKS_AT_EXTRACTION,)
    return tuple(
        # repr: both come from the Archive and may hold control characters.
        f"would skip symlink {link.path!r} -> {link.link_target!r}: "
        f"{_LINK_REASONS[link.reason]} (not supported)"
        for link in links
    )


def completion_line(outcome: RestoreOutcome, *, archive: str, site_root: str) -> str:
    """UX-S02-04: the last line of a successful text-mode Run."""
    what = f"Restore Mode {outcome.mode.value}, archive {archive}, site {site_root}"
    if outcome.dry_run:
        return f"Dry Run complete ({what}); nothing was changed"
    return f"restore complete ({what})"


def print_outcome(
    outcome: RestoreOutcome, *, archive: str, site_root: str, completed: bool
) -> None:
    """Exactly what ``outcome`` carries, on stdout (stderr for warnings).

    Under ``--log-format json`` stdout is one JSON object,
    ``{"restore": restore_summary(...)}``, and nothing else (UX-S02-04).
    Otherwise the human lines, ending — when ``completed`` — with
    :func:`completion_line`, so a Run that changed nothing worth reporting
    still says it finished.
    """
    for link in () if outcome.dry_run else outcome.skipped_links or ():
        print(
            # repr: both come from the Archive and may hold control characters.
            f"WARNING: symlink {link.path!r} -> {link.link_target!r} not restored: "
            "absolute and out-of-root symlinks are not supported",
            file=sys.stderr,
        )
    if configured_format() == "json":
        summary = restore_summary(
            outcome, archive=archive, site_root=site_root, completed=completed
        )
        emit_result(json.dumps({"restore": summary}))
        return
    if outcome.resolved_run_id is not None:
        verb = "would resolve" if outcome.dry_run else "resolved"
        emit_result(f"{verb} {outcome.resolved_run_id}")
        return
    if outcome.dry_run:
        # Owner decisions 64 and 65: what the Run chose, before the plan.
        emit_result("\n".join([*outcome.notes, *(a.text for a in outcome.actions)]))
        lines = planned_link_lines(outcome.skipped_links)
        if lines:
            emit_result("\n".join(lines))
    if outcome.database is not None:
        emit_database_report(outcome.database)
    if outcome.search_replace is not None:
        emit_result(search_replace_line(outcome.search_replace))
    if outcome.snapshot_path is not None:
        emit_result(f"safety snapshot: {outcome.snapshot_path}")
    if outcome.wp_config_kept:
        emit_result(
            "wp-config.php kept (target's); pass --overwrite-wp-config to replace it"
        )
    if completed:
        emit_result(completion_line(outcome, archive=archive, site_root=site_root))


def print_kept_snapshot(path: PurePath, *, error: BaseException) -> None:
    """A failed Run's Safety Snapshot, on stdout in the configured format."""
    if configured_format() == "json":
        failed = {
            "status": "failed",
            "error": type(error).__name__,
            "snapshot_path": str(path),
        }
        emit_result(json.dumps({"restore": failed}))
        return
    emit_result(f"safety snapshot: {path}")
