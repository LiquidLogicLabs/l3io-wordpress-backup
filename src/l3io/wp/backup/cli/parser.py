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
with an operator-facing reason; :func:`not_yet_implemented` names it, and the
root exits ``USAGE`` rather than ignoring it. Its ``--help`` line ends with
:data:`NOT_AVAILABLE_NOTE`, applied from the same mapping (UX-S02-03). The
story that binds each entry is named in a comment beside its reason, never
in text an operator reads.

**Secret values.** No flag takes a passphrase; passphrases come only from
AD-26's chain. ``--admin-password`` and ``--new-db-password`` are the two
frozen v0.2.21 password flags, kept on AD-22's allowance, and their help says
the value is visible in the process list and names the alternatives: the
administrator's variable, and ``--new-db-password-file`` or
:data:`NEW_DB_PASSWORD_VAR` (owner decision 2026-11-07), which keep the
relocated account's password out of ``argv``.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, NoReturn

from l3io.wp.backup import __version__
from l3io.wp.backup.adapters.credential_providers import ADMIN_PASSWORD_VAR
from l3io.wp.backup.adapters.database import ADMIN_TLS_MODES
from l3io.wp.backup.domain.exit_codes import DESCRIPTIONS, ExitCode

__all__ = [
    "BACKUP_COMMAND",
    "DEFAULT_LOG_LEVEL",
    "DOCTOR_COMMAND",
    "LOG_FORMATS",
    "LOG_LEVELS",
    "NEW_DB_PASSWORD_VAR",
    "NOT_AVAILABLE_NOTE",
    "NOT_YET_IMPLEMENTED",
    "RESTORE_COMMAND",
    "RESTORE_MODE_GROUP",
    "RESTORE_OPTIONS_GROUP",
    "VERIFY_COMMAND",
    "WP_DIR_REQUIRED",
    "CliParser",
    "build_parser",
    "exit_code_epilog",
    "not_yet_implemented",
    "require_backup_wp_dir",
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
#: The relocated account's password from the environment, not the command line.
NEW_DB_PASSWORD_VAR: Final = "L3IO_WP_BACKUP_NEW_DB_PASSWORD"  # noqa: S105 - a name

# Never bound: the legacy option had no meaning without its removed dependency.
_REMOVED = "this option was removed and will not return"
#: Ends the ``--help`` line of every :data:`NOT_YET_IMPLEMENTED` entry.
NOT_AVAILABLE_NOTE: Final = "(not available in this build)"

#: Each subcommand (``"restore"``) or subcommand flag (``"restore --force"``)
#: that is accepted but not yet bound, and why, in the operator's terms. Using
#: one exits ``USAGE`` naming both; the binding story removes its entries.
NOT_YET_IMPLEMENTED: Final[Mapping[str, str]] = MappingProxyType(
    {
        "restore --only-appointed-in-asg": _REMOVED,
    }
)
#: ``INFO``, not v0.2.21's ``CRITICAL``, so a Run's progress is visible by
#: default (a behaviour fix AD-22 permits).
DEFAULT_LOG_LEVEL = "INFO"
#: The ``--log-level`` names shown in help and in the error (any stdlib name,
#: in any case, is accepted).
LOG_LEVELS: Final = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
#: ``--log-format`` choices, as ``l3io-wp-database`` v0.2.0 spells them.
LOG_FORMATS = ("console", "json")


def _log_level(text: str) -> str:
    """A stdlib level name, case-insensitive; anything else is a usage error."""
    name = text.upper()
    if name == "NOTSET" or name not in logging.getLevelNamesMapping():
        choices = ", ".join(LOG_LEVELS[:-1]) + f" or {LOG_LEVELS[-1]}"
        msg = f"invalid level {text!r} (choose {choices})"
        raise argparse.ArgumentTypeError(msg)
    return name


def exit_code_epilog() -> str:
    """The exit-code table for ``--help``, generated from ``ExitCode`` (FR-21)."""
    rows = [
        f"  {int(code):>2}  {code.name:<19}  {DESCRIPTIONS[code]}" for code in ExitCode
    ]
    return "\n".join(["exit codes:", *rows])


class CliParser(argparse.ArgumentParser):
    """argparse, with a usage error naming its exit code as every failure does.

    BL-E002-058: the plain ``wpbackup2: error: ...`` line ends with the same
    ``[exit 2 USAGE: ...]`` suffix ``cli.exit.render_failure`` gives every
    other failure. Subcommand parsers are of this class too (argparse makes
    them of the parent's type). ``commands`` maps each subcommand to its
    parser, so a check after parsing reports with that command's usage
    (BL-E003-101), as argparse's own errors do.
    """

    commands: Mapping[str, argparse.ArgumentParser] = MappingProxyType({})

    def __init__(
        self,
        *,
        add_help: bool = True,
        **kwargs: Any,  # noqa: ANN401 - argparse's own keyword arguments
    ) -> None:
        # UX-01-11: every help line is capitalised, argparse's own -h included.
        super().__init__(add_help=False, **kwargs)
        if add_help:
            self.add_argument(
                "-h",
                "--help",
                action="help",
                default=argparse.SUPPRESS,
                help="Show this help message and exit",
            )

    def for_command(self, action: str | None) -> argparse.ArgumentParser:
        """The parser of ``action``'s subcommand; this one for none."""
        return self.commands.get(action or "", self)

    def error(self, message: str) -> NoReturn:
        code = ExitCode.USAGE
        self.print_usage(sys.stderr)
        suffix = f"[exit {int(code)} {code.name}: {DESCRIPTIONS[code]}]"
        self.exit(int(code), f"{self.prog}: error: {message} {suffix}\n")


def build_parser() -> CliParser:
    """Build the CLI parser with both frozen subcommands registered."""
    parser = CliParser(
        prog="wpbackup2",
        description="Backup/restore your WordPress content and database.",
        epilog=exit_code_epilog(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help="Show the program's version number and exit",
    )
    parser.add_argument(
        "--log-level",
        type=_log_level,
        default=DEFAULT_LOG_LEVEL,
        metavar="{" + ",".join(LOG_LEVELS) + "}",
        help=f"Logging level (stdlib level names; default {DEFAULT_LOG_LEVEL})",
    )
    parser.add_argument(
        "--log-format",
        choices=LOG_FORMATS,
        default=None,
        help="Log format on stderr (default: console on a terminal, else json)",
    )
    # AD-22 freezes the camelCase spelling; --what-if is an alias, an addition
    # AD-22 permits, and both set the one `what_if` dest.
    parser.add_argument(
        "--whatIf",
        "--what-if",
        dest="what_if",
        action="store_true",
        help=(
            "Dry Run: resolve credentials, run preflight and every query for "
            "real, and print the ordered plan; changes no Site, database or "
            "archive, but takes the Site Lock (exit 9 LOCKED while another Run "
            "holds it)"
        ),
    )

    subparsers = parser.add_subparsers(dest="action", help="Action being requested")
    _add_backup(_command(subparsers, BACKUP_COMMAND, "Perform a WordPress backup"))
    _add_restore(_command(subparsers, RESTORE_COMMAND, "Perform a WordPress restore"))
    _add_verify(
        _command(subparsers, VERIFY_COMMAND, "Check an archive without touching a site")
    )
    _add_doctor(
        _command(
            subparsers,
            DOCTOR_COMMAND,
            "Report whether this host is ready to back up and restore",
        )
    )
    for command, command_parser in subparsers.choices.items():
        _note_unavailable_flags(command, command_parser)
    parser.commands = MappingProxyType(dict(subparsers.choices))
    return parser


def _command(
    subparsers: argparse._SubParsersAction[CliParser],
    name: str,
    text: str,
) -> argparse.ArgumentParser:
    """A subcommand whose ``--help`` has its description and the exit codes."""
    return subparsers.add_parser(
        name,
        help=_noted(name, text),
        description=_noted(name, text),
        epilog=exit_code_epilog(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )


def _noted(key: str, text: str) -> str:
    """``text``, ending with :data:`NOT_AVAILABLE_NOTE` when ``key`` is unbound."""
    return f"{text} {NOT_AVAILABLE_NOTE}" if key in NOT_YET_IMPLEMENTED else text


def _note_unavailable_flags(
    command: str, command_parser: argparse.ArgumentParser
) -> None:
    """UX-S02-03: mark each unbound flag's help, derived from the mapping."""
    for action in command_parser._actions:  # noqa: SLF001 - argparse has no public API
        for flag in action.option_strings:
            if f"{command} {flag}" in NOT_YET_IMPLEMENTED and action.help:
                action.help = _noted(f"{command} {flag}", action.help)
                break


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


#: argparse's own message for a missing required flag, kept verbatim for
#: ``--wp-dir`` (BL-E008-013): optional only with ``--list-default-excludes``.
WP_DIR_REQUIRED: Final = "the following arguments are required: --wp-dir"


def require_backup_wp_dir(values: Mapping[str, object]) -> str | None:
    """The usage message when a backup lacks ``--wp-dir``, else ``None``.

    BL-E008-013 (owner decision 2026-11-07): the parser registers ``--wp-dir``
    as optional so ``--list-default-excludes`` runs without it; every other
    backup still needs it, with argparse's own message and exit 2.
    """
    if values.get("wp_dir") is None and not values.get("list_default_excludes"):
        return WP_DIR_REQUIRED
    return None


def _add_backup(backup: argparse.ArgumentParser) -> None:
    # Required after parsing, except with --list-default-excludes
    # (require_backup_wp_dir; BL-E008-013).
    backup.add_argument(
        "--wp-dir",
        help=(
            "Path to the root of the WordPress directory (required, except "
            "with --list-default-excludes)"
        ),
        default=None,
    )
    # UX-1 (BL-E002-101): as on restore, an addition AD-22 permits; the frozen
    # spelling stays on the top-level parser. SUPPRESS: absent here, the
    # top-level value stands (a default would overwrite `--whatIf backup`).
    backup.add_argument(
        "--whatIf",
        "--what-if",
        dest="what_if",
        action="store_true",
        default=argparse.SUPPRESS,
        help=(
            "Dry Run: run every check and query for real and print the ordered "
            "plan; writes no archive but takes the Site Lock (also accepted "
            "before `backup`)"
        ),
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
        help="Use this wp-config.php instead of searching for it",
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
        help=(
            "Print the default exclusion set and exit without backing up "
            "(needs no --wp-dir; takes no other backup flag)"
        ),
    )
    # Owner decisions 2026-11-07 (AD-22 additions): BL-E002-063, BL-E002-064.
    backup.add_argument(
        "--confirm-db-host",
        metavar="HOST",
        default=None,
        help=(
            "Confirm a database host that is not local (anything but localhost, "
            "127.0.0.1, ::1 or a socket): name wp-config.php's DB_HOST host, as "
            "resolved from the environment for a getenv_docker(...) one, or "
            "the backup is refused before any connection"
        ),
    )
    backup.add_argument(
        "--allow-target-in-site-root",
        action="store_true",
        help=(
            "Write the archive inside the Site Root (refused by default: the web "
            "server may serve it); prefer a target outside the Site Root"
        ),
    )
    _add_db_credentials(backup)


def _add_restore(restore: argparse.ArgumentParser) -> None:
    restore.add_argument(
        "--wp-dir", help="Path to the root of the WordPress directory", required=True
    )
    restore.add_argument(
        "--archive",
        help=(
            "Path or file: URL of the archive to restore from (required, "
            "--resolve-interrupted-run included)"
        ),
        default=None,
        required=True,
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
            "over. Valid as --mode db --drop-database, or with --mode all "
            "--cleanfirst; alone (the default --mode all) it is refused"
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

    options = restore.add_argument_group(
        RESTORE_OPTIONS_GROUP,
        "The flags that do not select the Restore Mode: Dry Run, wp-config.php, "
        "the Safety Snapshot, the database administrator and relocation",
    )
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
            "Dry Run: run every check and query for real and print the ordered "
            "plan; changes no Site or database but takes the Site Lock (also "
            "accepted before `restore`)"
        ),
    )
    options.add_argument(
        "--wp-config",
        metavar="PATH",
        default=None,
        help="Use this wp-config.php instead of searching for it",
    )
    options.add_argument(
        "--overwrite-wp-config",
        action="store_true",
        help=(
            "Replace the target's existing wp-config.php with the archive's "
            "(only with a mode that restores files), keeping the target's DB_* "
            "definitions as written, getenv_docker(...) included. Refused "
            "(exit 3) when the target uses getenv_docker(...) and the archive's "
            "wp-config.php does not define it"
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
        "--require-encrypted",
        action="store_true",
        help=(
            "Refuse an archive that is not encrypted, before anything changes "
            "(guards against a plain archive substituted for an encrypted one; "
            "without it a plain archive is restored with a warning)"
        ),
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
        choices=ADMIN_TLS_MODES,
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
        help=(
            "Resolve the journal left by the interrupted Run with this ID, then "
            "stop: nothing is restored (run the restore again afterwards)"
        ),
    )
    options.add_argument(
        "--admin-username",
        metavar="USER",
        default=None,
        help="Database administrator username (give --admin-password with it)",
    )
    options.add_argument(
        "--admin-password",
        metavar="PASSWORD",
        default=None,
        help=(
            "Database administrator password. The value is visible in the "
            f"process list; prefer {ADMIN_PASSWORD_VAR}"
        ),
    )
    options.add_argument(
        "--admin-credentials-aws-secret-id",
        metavar="ID",
        default=None,
        help="Read the database administrator from this AWS Secrets Manager secret",
    )
    options.add_argument(
        "--admin-credentials-aws-region",
        metavar="REGION",
        default=None,
        help="AWS region of --admin-credentials-aws-secret-id (needs the secret id)",
    )
    options.add_argument(
        "--only-appointed-in-asg",
        action="store_true",
        help="Removed; accepted only to say so",
    )
    options.add_argument(
        "--new-site-url",
        metavar="URL",
        default=None,
        help="Relocate the Site to this URL",
    )
    options.add_argument(
        "--new-site-home-url",
        metavar="URL",
        default=None,
        help="Relocate the Site's home URL",
    )
    options.add_argument(
        "--new-db-host",
        metavar="HOST",
        default=None,
        help="Relocate to this database host",
    )
    options.add_argument(
        "--new-db-name",
        metavar="NAME",
        default=None,
        help="Relocate to this database name",
    )
    options.add_argument(
        "--new-db-user",
        metavar="USER",
        default=None,
        help="Relocate to this database user",
    )
    options.add_argument(
        "--new-db-password",
        metavar="PASSWORD",
        default=None,
        help=(
            "Password of --new-db-user. The value is visible in the process "
            f"list; prefer --new-db-password-file or {NEW_DB_PASSWORD_VAR}"
        ),
    )
    options.add_argument(
        "--new-db-password-file",
        metavar="PATH",
        type=Path,
        default=None,
        help=(
            "Read the password of --new-db-user from this file (its first "
            "line; make it readable only by you)"
        ),
    )
    # Owner decision 2026-11-07 (BL-E002-098; an AD-22 addition).
    options.add_argument(
        "--allow-root",
        action="store_true",
        help=(
            "Restore even though running as root (refused by default: "
            "extraction as root can be raced by a symlink swap in the Site Root)"
        ),
    )
    options.add_argument(
        "--allow-unencrypted",
        action="store_true",
        help=(
            "Restore an archive that is not encrypted even though a passphrase "
            "was given (without it, that is refused as a possible substitution)"
        ),
    )
    _add_db_credentials(restore)


def _add_verify(verify: argparse.ArgumentParser) -> None:
    # BL-E002-077 (owner decision 2026-11-07): accepted in either position and
    # answered with a note, never silently ignored. SUPPRESS: absent here, the
    # top-level value stands.
    verify.add_argument(
        "--whatIf",
        "--what-if",
        dest="what_if",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Accepted for symmetry: verify is read-only, so it changes nothing",
    )
    verify.add_argument(
        "--archive", help="Path or file: URL of the archive to check", required=True
    )
    verify.add_argument(
        "--authenticate",
        action="store_true",
        help=(
            "Decrypt the encrypted payload to prove the archive authentic; "
            "without it, verify proves only that its checksums are consistent. "
            "The passphrase comes from L3IO_WP_BACKUP_PASSPHRASE or a prompt. "
            "It does not show the archive is the latest one: an older archive "
            "under the same passphrase also passes"
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
