"""The argparse parser — the single source of truth for the CLI surface.

AD-22 freezes that surface: the ``backup`` and ``restore`` subcommands, all
nineteen v0.2.21 flag spellings (including the camelCase ``--whatIf``) on the
subcommands they lived on, and ``python -m wpbackup2`` as a working
invocation. Additions are permitted; no frozen spelling is renamed or removed.
``tests/contracts/test_cli_surface.py`` checks the frozen set against the
legacy parser, read by AST.

**Restore flags are classified where they are defined (AD-25).** The
mode-selecting flags sit in the :data:`RESTORE_MODE_GROUP` argument group and
every other restore flag in :data:`RESTORE_OPTIONS_GROUP`;
``tests/contracts/test_flag_map.py`` derives its scope from the first group,
so a flag added there without an entry in
``l3io.wp.backup.domain.flag_map`` fails CI, and a flag in neither group fails
too.

**Accepted and explained (AD-22).** A flag or subcommand whose behaviour has
not landed is registered anyway and listed in :data:`NOT_YET_IMPLEMENTED`
with the story that binds it; :func:`not_yet_implemented` names it, and the
root exits ``USAGE`` rather than ignoring it.

**Secret values.** No flag takes a passphrase; passphrases come only from
AD-26's chain. ``--admin-password`` and ``--new-db-password`` are the two
frozen v0.2.21 password flags, kept on AD-22's allowance, and their help says
the value is visible in the process list.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from l3io.wp.backup import __version__
from l3io.wp.backup.domain.exit_codes import DESCRIPTIONS, ExitCode

__all__ = [
    "BACKUP_COMMAND",
    "DEFAULT_LOG_LEVEL",
    "DOCTOR_COMMAND",
    "LOG_FORMATS",
    "NOT_YET_IMPLEMENTED",
    "RESTORE_COMMAND",
    "RESTORE_MODE_GROUP",
    "RESTORE_OPTIONS_GROUP",
    "VERIFY_COMMAND",
    "build_parser",
    "exit_code_epilog",
    "not_yet_implemented",
]

BACKUP_COMMAND = "backup"
RESTORE_COMMAND = "restore"
#: An addition AD-22 permits (FR-3): check an Archive without touching a Site.
VERIFY_COMMAND = "verify"
#: An addition AD-22 permits (FR-27): report whether a host is ready.
DOCTOR_COMMAND = "doctor"
#: The argument group holding the restore flags AD-25's table maps.
RESTORE_MODE_GROUP = "restore mode"
#: The argument group holding every other restore flag.
RESTORE_OPTIONS_GROUP = "restore options"
#: The administrator's password from the environment, not the command line.
ADMIN_PASSWORD_VARIABLE = "L3IO_WP_BACKUP_ADMIN_PASSWORD"  # noqa: S105 - a name

_RELOCATION_BINDER = "delivered by Epic 7 (relocation)"
_ROOT_BINDER = "delivered by E008-S01-001"

#: Each subcommand (``"restore"``) or subcommand flag (``"restore --force"``)
#: that is accepted but not yet bound, and the story that binds it. Using one
#: exits ``USAGE`` naming both; the binding story removes its entries.
NOT_YET_IMPLEMENTED: Final[Mapping[str, str]] = MappingProxyType(
    {
        "restore --wp-config": _RELOCATION_BINDER,
        "restore --new-site-url": _RELOCATION_BINDER,
        "restore --new-site-home-url": _RELOCATION_BINDER,
        "restore --new-db-host": _RELOCATION_BINDER,
        "restore --new-db-name": _RELOCATION_BINDER,
        "restore --new-db-user": _RELOCATION_BINDER,
        "restore --new-db-password": _RELOCATION_BINDER,
        "restore --admin-credentials-aws-secret-id": _ROOT_BINDER,
        "restore --admin-credentials-aws-region": _ROOT_BINDER,
        "restore --db-credentials-aws-secret-id": _ROOT_BINDER,
        "restore --db-credentials-aws-region": _ROOT_BINDER,
        "restore --only-appointed-in-asg": (
            "removed with `chesney`; it will not be implemented"
        ),
        "backup --encrypt-with-passphrase": _ROOT_BINDER,
        "backup --exclude": _ROOT_BINDER,
        "backup --no-default-excludes": _ROOT_BINDER,
        "backup --list-default-excludes": _ROOT_BINDER,
        "backup --db-credentials-aws-secret-id": _ROOT_BINDER,
        "backup --db-credentials-aws-region": _ROOT_BINDER,
        DOCTOR_COMMAND: _ROOT_BINDER,
    }
)
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
    # AD-22 freezes the camelCase spelling; --what-if is its alias (triage E-7).
    parser.add_argument(
        "--whatIf",
        "--what-if",
        dest="what_if",
        action="store_true",
        help=(
            "Dry Run: resolve credentials, run preflight and every query for "
            "real, print the ordered plan, and change nothing"
        ),
    )

    subparsers = parser.add_subparsers(dest="action", help="Action being requested")
    _add_backup(
        subparsers.add_parser(BACKUP_COMMAND, help="Perform a WordPress backup")
    )
    _add_restore(
        subparsers.add_parser(RESTORE_COMMAND, help="Perform a WordPress restore")
    )
    _add_verify(
        subparsers.add_parser(
            VERIFY_COMMAND, help="Check an archive without touching a site"
        )
    )
    _add_doctor(
        subparsers.add_parser(
            DOCTOR_COMMAND,
            help="Report whether this host is ready to back up and restore",
        )
    )
    return parser


def _add_db_credentials(parser: argparse.ArgumentParser) -> None:
    """FR-14: the WordPress account from an AWS secret, mirroring the admin pair."""
    group = parser.add_argument_group("database credentials")
    group.add_argument(
        "--db-credentials-aws-secret-id",
        metavar="ID",
        default=None,
        help="Read the WordPress database account from this AWS Secrets Manager secret",
    )
    group.add_argument(
        "--db-credentials-aws-region",
        metavar="REGION",
        default=None,
        help="AWS region of --db-credentials-aws-secret-id (needs the secret id)",
    )


def _add_backup(backup: argparse.ArgumentParser) -> None:
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
    backup.add_argument(
        "--encrypt-with-passphrase",
        action="store_true",
        help=(
            "Encrypt the archive with a passphrase. The passphrase comes from "
            "L3IO_WP_BACKUP_PASSPHRASE or a prompt, never the command line"
        ),
    )
    backup.add_argument(
        "--exclude",
        metavar="PATTERN",
        action="append",
        default=None,
        help="Leave matching paths out of the archive (repeatable)",
    )
    backup.add_argument(
        "--no-default-excludes",
        action="store_true",
        help="Do not apply the default exclusion set",
    )
    backup.add_argument(
        "--list-default-excludes",
        action="store_true",
        help="Print the default exclusion set and exit without backing up",
    )
    _add_db_credentials(backup)


def _add_restore(restore: argparse.ArgumentParser) -> None:
    restore.add_argument(
        "--wp-dir", help="Path to the root of the WordPress directory", required=True
    )
    restore.add_argument(
        "--archive",
        help="Path or file: URL of the archive to restore from",
        default=None,
        required=False,
    )

    mode = restore.add_argument_group(
        RESTORE_MODE_GROUP,
        "Together these select one Restore Mode; any other combination is refused.",
    )
    mode.add_argument(
        "--mode",
        choices=["files", "db", "all"],
        default="all",
        help="What to restore: files, db, or all (default: all)",
    )
    mode.add_argument(
        "--cleanfirst",
        action="store_true",
        help=(
            "Clear the Site Root before restoring the files. It does not "
            "remove the database: that is --drop-database"
        ),
    )
    mode.add_argument(
        "--drop-database",
        action="store_true",
        help=(
            "Remove the previous database once the restored one has taken "
            "over. --mode all needs --cleanfirst with it"
        ),
    )
    mode.add_argument(
        "--force",
        action="store_true",
        help=(
            "Restore into a non-empty target for the parts the mode restores. "
            "It selects no mode and removes nothing"
        ),
    )

    options = restore.add_argument_group(RESTORE_OPTIONS_GROUP)
    # UX-S02-02: an addition AD-22 permits; the frozen spelling stays on the
    # top-level parser. SUPPRESS: absent here, the top-level value stands
    # (a subparser's default would overwrite `wpbackup2 --whatIf restore`).
    options.add_argument(
        "--whatIf",
        "--what-if",
        dest="what_if",
        action="store_true",
        default=argparse.SUPPRESS,
        help=(
            "Dry Run: run every check and query for real, print the ordered "
            "plan, and change nothing (also accepted before `restore`)"
        ),
    )
    options.add_argument(
        "--wp-config",
        metavar="PATH",
        default=None,
        help="use this wp-config.php instead of searching for it",
    )
    options.add_argument(
        "--overwrite-wp-config",
        action="store_true",
        help=(
            "Replace the target's existing wp-config.php with the archive's "
            "(only with a mode that restores files)"
        ),
    )
    options.add_argument(
        "--reset-existing-db-user",
        action="store_true",
        help=(
            "When the target has no wp-config.php and the archive's database "
            "account already exists: reset its password to the archive's "
            "(refused otherwise)"
        ),
    )
    options.add_argument(
        "--no-safety-snapshot",
        action="store_true",
        help="Do not take the Safety Snapshot before changing anything",
    )
    options.add_argument(
        "--snapshot-dir",
        metavar="PATH",
        default=None,
        help=(
            "Directory to write the Safety Snapshot to (default: the per-user "
            "state directory). Refused inside the Site Root or the wp-config.php "
            "directory; a directory a web server serves from elsewhere is not "
            "detected"
        ),
    )
    options.add_argument(
        "--archive-database",
        metavar="USER@HOST[:PORT]/NAME",
        default=None,
        help=(
            "When the target has no wp-config.php: confirm the database the "
            "archive's config names (user, host, port and name must all match), "
            "or the restore is refused"
        ),
    )
    options.add_argument(
        "--admin-tls",
        choices=["required", "verify-ca", "verify-identity"],
        default=None,
        help=(
            "Raise the TLS the database connections use (default: verified TLS "
            "to any host but localhost, 127.0.0.1 and ::1; never lowered)"
        ),
    )
    options.add_argument(
        "--admin-tls-ca",
        metavar="FILE",
        default=None,
        help="CA bundle that verifies the database server (default: the system store)",
    )
    options.add_argument(
        "--resolve-interrupted-run",
        metavar="RUN_ID",
        default=None,
        help="Resolve the journal left by the interrupted Run with this ID",
    )
    options.add_argument(
        "--admin-username",
        default=None,
        help="Database administrator username (give --admin-password with it)",
    )
    options.add_argument(
        "--admin-password",
        default=None,
        help=(
            "Database administrator password. The value is visible in the "
            f"process list; prefer {ADMIN_PASSWORD_VARIABLE}"
        ),
    )
    options.add_argument(
        "--admin-credentials-aws-secret-id",
        default=None,
        help="Read the database administrator from this AWS Secrets Manager secret",
    )
    options.add_argument(
        "--admin-credentials-aws-region",
        default=None,
        help="AWS region of --admin-credentials-aws-secret-id (needs the secret id)",
    )
    options.add_argument(
        "--only-appointed-in-asg",
        action="store_true",
        help="Removed with chesney; accepted only to explain that",
    )
    options.add_argument(
        "--new-site-url", default=None, help="Relocate the Site to this URL"
    )
    options.add_argument(
        "--new-site-home-url", default=None, help="Relocate the Site's home URL"
    )
    options.add_argument(
        "--new-db-host", default=None, help="Relocate to this database host"
    )
    options.add_argument(
        "--new-db-name", default=None, help="Relocate to this database name"
    )
    options.add_argument(
        "--new-db-user", default=None, help="Relocate to this database user"
    )
    options.add_argument(
        "--new-db-password",
        default=None,
        help=("Password of --new-db-user. The value is visible in the process list"),
    )
    _add_db_credentials(restore)


def _add_verify(verify: argparse.ArgumentParser) -> None:
    verify.add_argument(
        "--archive", help="Path or file: URL of the archive to check.", required=True
    )
    verify.add_argument(
        "--authenticate",
        action="store_true",
        help=(
            "Decrypt the encrypted payload to prove the archive authentic; "
            "without it, verify proves only that its checksums are consistent. "
            "The passphrase comes from L3IO_WP_BACKUP_PASSPHRASE or a prompt."
        ),
    )


def _add_doctor(doctor: argparse.ArgumentParser) -> None:
    doctor.add_argument(
        "--wp-dir", metavar="DIR", default=None, help="Also check this Site Root"
    )
    doctor.add_argument(
        "--archive",
        metavar="TARGET",
        default=None,
        help="Also check this archive target",
    )
    doctor.add_argument(
        "--json", action="store_true", help="Print the diagnosis as JSON"
    )


def _dest(flag: str) -> str:
    """argparse's documented ``dest`` for a long option: dashes become underscores."""
    return flag.lstrip("-").replace("-", "_")


def not_yet_implemented(command: str, values: Mapping[str, object]) -> str | None:
    """The usage message for the first unbound feature used, if any.

    ``values`` are the parsed arguments by ``dest``.

    Flag entries are checked before the subcommand's own entry, so a flag
    bound by a later story than its subcommand is named with that story. A
    flag counts as used when its value is neither ``None`` nor ``False`` —
    every listed flag defaults to one of those.
    """
    for key, binder in NOT_YET_IMPLEMENTED.items():
        entry_command, _, flag = key.partition(" ")
        if flag and entry_command == command:
            value = values[_dest(flag)]
            if value is not None and value is not False:
                return f"{command} {flag} is not available in this build: {binder}"
    command_binder = NOT_YET_IMPLEMENTED.get(command)
    if command_binder is not None:
        return f"wpbackup2 {command} is not available in this build: {command_binder}"
    return None
