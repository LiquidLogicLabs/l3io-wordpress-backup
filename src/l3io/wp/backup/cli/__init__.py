"""The argparse composition root.

Configures logging once (AD-10, ``cli/logging.py``) before any Run opens,
creates the Run's ``SecretRegistry``, then dispatches: ``backup`` to
:func:`l3io.wp.backup.cli.backup.run_backup`, ``restore`` to
:func:`l3io.wp.backup.cli.restore.run_restore`, ``verify`` to
:func:`l3io.wp.backup.cli.verify.run_verify` and ``doctor`` to
:func:`l3io.wp.backup.cli.doctor.run_doctor`, whose drivers open the Run
Context (AD-16) with that registry. Anything else is refused rather than
pretending to act.

**Before any Run opens** the arguments are checked: a restore flag
combination that expresses no Restore Mode (AD-25), an incomplete credential
flag pair, or a flag or subcommand still in
:data:`~l3io.wp.backup.cli.parser.NOT_YET_IMPLEMENTED` exits ``USAGE`` through
argparse with the table's or the entry's message, so it carries no ``run_id``.

**Exit codes (FR-21).** :func:`main` returns ``int(ExitCode.X)`` on every
path. A ``run_*`` returns ``ExitCode.OK`` or raises; the root maps what it
raised with :func:`~l3io.wp.backup.cli.exit.exit_code_for` and reports it
through the registry the Run used (ADR-0018), so a defect's traceback is
redacted too. Usage errors are argparse's own ``SystemExit(2)``, which is
``ExitCode.USAGE``. Ctrl-C (``KeyboardInterrupt``) during a Run is reported
by :func:`~l3io.wp.backup.cli.exit.report_interrupt` — one line, no traceback —
and exits ``130`` (:data:`~l3io.wp.backup.domain.exit_codes.INTERRUPTED_EXIT`),
after the Run's ``finally`` chain has released its lock and workspace.

**SIGTERM (FR-28).** For the duration of the Run only, :func:`main` installs a
SIGTERM handler — and the same for SIGHUP and SIGQUIT
(:data:`TERMINATING_SIGNALS`; owner decision 2026-11-07) — raising
:class:`~l3io.wp.backup.domain.errors.RunTerminated` (``FAILED``), so the Run's
``finally`` chain releases the Site Lock and removes the workspace; SIGINT
already raises ``KeyboardInterrupt``. The previous handler is restored
afterwards. Off the main thread nothing is installed (Python allows handlers
only there). The library root installs none: a host owns its process, and the
kernel releases the ``flock`` of one that is killed.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from types import FrameType

from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.cli.exit import report_failure, report_interrupt
from l3io.wp.backup.cli.logging import configure_logging
from l3io.wp.backup.cli.parser import (
    BACKUP_COMMAND,
    DOCTOR_COMMAND,
    RESTORE_COMMAND,
    VERIFY_COMMAND,
    build_parser,
    not_yet_implemented,
    require_backup_wp_dir,
)
from l3io.wp.backup.domain.errors import RunTerminated, UsageError
from l3io.wp.backup.domain.exit_codes import ExitCode

__all__ = ["TERMINATING_SIGNALS", "main", "run_from_cli"]


def _terminate(signum: int, frame: FrameType | None) -> None:
    """Raise once: every terminating signal is ignored from here on.

    A second signal (a supervisor's retry, a stop pressed twice) would raise
    again inside the cleanup this one starts and could abort the workspace's
    removal part-way (BL-E002-081). :func:`_sigterm_raises` restores the
    previous handlers when the Run is over.
    """
    del frame
    for sig in TERMINATING_SIGNALS:
        signal.signal(sig, signal.SIG_IGN)
    raise RunTerminated(signal.Signals(signum).name)


#: The signals handled like SIGTERM (BL-E002-065, owner decision 2026-11-07:
#: a closed terminal or Ctrl-backslash cleans up too); those this platform lacks are
#: skipped.
TERMINATING_SIGNALS: tuple[signal.Signals, ...] = tuple(
    getattr(signal, name)
    for name in ("SIGTERM", "SIGHUP", "SIGQUIT")
    if hasattr(signal, name)
)


@contextmanager
def _sigterm_raises() -> Iterator[None]:
    """Each terminating signal raises :class:`RunTerminated`, then is restored."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = {sig: signal.signal(sig, _terminate) for sig in TERMINATING_SIGNALS}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main(argv: Sequence[str] | None = None) -> int:
    """Parse ``argv`` and dispatch. Returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.action is None:
        parser.print_help(sys.stderr)
        return int(ExitCode.USAGE)
    # BL-E003-101: a check after parsing reports with its command's usage.
    usage = parser.for_command(args.action)
    try:
        if args.action == RESTORE_COMMAND:
            from l3io.wp.backup.cli.restore import check_restore_arguments

            check_restore_arguments(
                mode=args.mode,
                cleanfirst=args.cleanfirst,
                drop_database=args.drop_database,
                force=args.force,
                admin_username=args.admin_username,
                admin_password=args.admin_password,
                admin_aws_secret_id=args.admin_credentials_aws_secret_id,
                admin_aws_region=args.admin_credentials_aws_region,
                db_aws_secret_id=args.db_credentials_aws_secret_id,
                db_aws_region=args.db_credentials_aws_region,
                environ=os.environ,
            )
        elif args.action == BACKUP_COMMAND:
            from l3io.wp.backup.cli.credentials import wordpress_spec

            missing = require_backup_wp_dir(vars(args))
            if missing is not None:
                usage.error(missing)

            wordpress_spec(
                aws_secret_id=args.db_credentials_aws_secret_id,
                aws_region=args.db_credentials_aws_region,
            )
    except UsageError as exc:
        usage.error(str(exc))
    unavailable = not_yet_implemented(args.action, vars(args))
    if unavailable is not None:
        usage.error(unavailable)
    if args.action == RESTORE_COMMAND and not args.archive:
        usage.error("restore needs --archive (the archive to restore from)")
    if args.action not in (
        BACKUP_COMMAND,
        RESTORE_COMMAND,
        VERIFY_COMMAND,
        DOCTOR_COMMAND,
    ):
        usage.error(f"{args.action!r} is not implemented in this build")
    configure_logging(args.log_level, args.log_format)
    secrets = SecretRegistry()
    try:
        with _sigterm_raises():
            if args.action == BACKUP_COMMAND:
                from l3io.wp.backup.cli.backup import run_backup

                code = run_backup(args, secrets=secrets)
            elif args.action == RESTORE_COMMAND:
                from l3io.wp.backup.cli.restore import run_restore

                code = run_restore(args, secrets=secrets)
            elif args.action == DOCTOR_COMMAND:
                from l3io.wp.backup.cli.doctor import run_doctor

                code = run_doctor(args, secrets=secrets)
            else:
                from l3io.wp.backup.cli.verify import run_verify

                code = run_verify(args, secrets=secrets)
    except KeyboardInterrupt as exc:
        return report_interrupt(exc, secrets=secrets)
    except Exception as exc:  # RunTerminated: mapped here (exit code)
        return int(report_failure(exc, secrets=secrets))
    return int(code)


def run_from_cli() -> None:
    """Console-script entry point.

    A closed stdout (``| head``) does not mask the Run's exit code: the
    ``BrokenPipeError`` is caught and stdout pointed at ``/dev/null`` so the
    interpreter's own flush at exit cannot raise again.
    """
    code = int(ExitCode.FAILED)
    try:
        code = main()
        sys.stdout.flush()
    except BrokenPipeError:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    raise SystemExit(code)
