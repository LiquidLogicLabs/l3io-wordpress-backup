"""The argparse parser — the single source of truth for the CLI surface.

AD-22 freezes that surface: both subcommands, all nineteen flag spellings
(including the camelCase ``--whatIf``), and ``python -m wpbackup2`` as a
working invocation. This module is deliberately incomplete at this stage: the
packaging skeleton and its contract gates land first, so that AD-25's gate is
proven to fire *before* there is a flag for it to catch.

AD-25's contract test derives its scope from this parser rather than from a
hand-kept list, so a flag added here without an entry in
``l3io.wp.backup.domain.flag_map`` fails CI.
"""

from __future__ import annotations

import argparse
import logging

from l3io.wp.backup import __version__
from l3io.wp.backup.domain.exit_codes import DESCRIPTIONS, ExitCode

__all__ = [
    "BACKUP_COMMAND",
    "DEFAULT_LOG_LEVEL",
    "LOG_FORMATS",
    "RESTORE_COMMAND",
    "VERIFY_COMMAND",
    "build_parser",
    "exit_code_epilog",
]

BACKUP_COMMAND = "backup"
RESTORE_COMMAND = "restore"
#: An addition AD-22 permits (FR-3): check an Archive without touching a Site.
VERIFY_COMMAND = "verify"
#: Triage E-14: ``INFO``, not v0.2.21's ``CRITICAL`` (a behaviour fix AD-22 permits).
DEFAULT_LOG_LEVEL = "INFO"
#: ``--log-format`` choices, as ``l3io-wp-database`` v0.2.0 spells them.
LOG_FORMATS = ("console", "json")


def _log_level(text: str) -> str:
    """A stdlib level name, case-insensitive; anything else is a usage error."""
    name = text.upper()
    if name == "NOTSET" or name not in logging.getLevelNamesMapping():
        msg = f"invalid level {text!r} (choose DEBUG, INFO, WARNING, ERROR or CRITICAL)"
        raise argparse.ArgumentTypeError(msg)
    return name


def exit_code_epilog() -> str:
    """The exit-code table for ``--help``, generated from ``ExitCode`` (FR-21)."""
    rows = [
        f"  {int(code):>2}  {code.name:<19}  {DESCRIPTIONS[code]}" for code in ExitCode
    ]
    return "\n".join(["exit codes:", *rows])


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser with both frozen subcommands registered."""
    parser = argparse.ArgumentParser(
        prog="wpbackup2",
        description="Backup/restore your WordPress content and database.",
        epilog=exit_code_epilog(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "--log-level",
        type=_log_level,
        default=DEFAULT_LOG_LEVEL,
        help=f"Logging level (stdlib level names; default {DEFAULT_LOG_LEVEL})",
    )
    parser.add_argument(
        "--log-format",
        choices=LOG_FORMATS,
        default=None,
        help="Log format on stderr (default: console on a terminal, else json)",
    )

    subparsers = parser.add_subparsers(dest="action", help="Action being requested")
    backup = subparsers.add_parser(BACKUP_COMMAND, help="Perform a WordPress backup")
    backup.add_argument(
        "--wp-dir", help="Path to the root of the WordPress directory", required=True
    )
    backup.add_argument(
        "--archive",
        help=(
            "Archive path or file: URL "
            "(default: ./wpbackup2-<db>-<UTC timestamp>.tar.gz)"
        ),
        default=None,
        required=False,
    )
    backup.add_argument(
        "--wp-config",
        metavar="PATH",
        help="use this wp-config.php instead of searching for it",
        default=None,
    )
    backup.add_argument(
        "--mode",
        choices=["files", "db", "all"],
        default="all",
        help="What to back up: files, db, or all (default: all)",
    )
    subparsers.add_parser(RESTORE_COMMAND, help="Perform a WordPress restore")
    verify = subparsers.add_parser(
        VERIFY_COMMAND, help="Check an archive without touching a site"
    )
    verify.add_argument(
        "--archive", help="Path or file: URL of the archive to check.", required=True
    )
    return parser
