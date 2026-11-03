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

from l3io.wp.backup import __version__

__all__ = ["BACKUP_COMMAND", "RESTORE_COMMAND", "VERIFY_COMMAND", "build_parser"]

BACKUP_COMMAND = "backup"
RESTORE_COMMAND = "restore"
#: An addition AD-22 permits (FR-3): check an Archive without touching a Site.
VERIFY_COMMAND = "verify"


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser with both frozen subcommands registered."""
    parser = argparse.ArgumentParser(
        prog="wpbackup2",
        description="Backup/restore your WordPress content and database.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )

    subparsers = parser.add_subparsers(dest="action", help="Action being requested")
    backup = subparsers.add_parser(BACKUP_COMMAND, help="Perform a WordPress backup")
    backup.add_argument(
        "--wp-dir", help="Path to the root of the WordPress directory", required=True
    )
    backup.add_argument(
        "--archive",
        help="Path and filename of the archive (.tar.gz) to backup to/restore from.",
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
        help="Indicate what to be Backup/Restore",
    )
    subparsers.add_parser(RESTORE_COMMAND, help="Perform a WordPress restore")
    verify = subparsers.add_parser(
        VERIFY_COMMAND, help="Check an archive without touching a site"
    )
    verify.add_argument(
        "--archive", help="Path or file: URL of the archive to check.", required=True
    )
    return parser
