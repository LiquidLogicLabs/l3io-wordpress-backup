"""The argparse composition root.

Configures logging once (AD-10, ``cli/logging.py``) before any Run opens,
creates the Run's ``SecretRegistry``, then dispatches: ``backup`` to
:func:`l3io.wp.backup.cli.backup.run_backup` and ``verify`` to
:func:`l3io.wp.backup.cli.verify.run_verify`, whose drivers open the Run
Context (AD-16) with that registry. Anything else is refused rather than
pretending to act.

**Exit codes (FR-21).** :func:`main` returns ``int(ExitCode.X)`` on every
path. A ``run_*`` returns ``ExitCode.OK`` or raises; the root maps what it
raised with :func:`~l3io.wp.backup.cli.exit.exit_code_for` and reports it
through the registry the Run used (ADR-0018), so a defect's traceback is
redacted too. Usage errors are argparse's own ``SystemExit(2)``, which is
``ExitCode.USAGE``. ``KeyboardInterrupt`` is not caught.

**SIGTERM (FR-28).** For the duration of the Run only, :func:`main` installs a
SIGTERM handler raising :class:`~l3io.wp.backup.domain.errors.RunTerminated`
(``FAILED``), so the Run's ``finally`` chain releases the Site Lock and removes
the workspace; SIGINT already raises ``KeyboardInterrupt``. The previous
handler is restored afterwards. Off the main thread nothing is installed
(Python allows handlers only there). The library root installs none: a host
owns its process, and the kernel releases the ``flock`` of one that is killed.
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
from l3io.wp.backup.cli.exit import report_failure
from l3io.wp.backup.cli.logging import configure_logging
from l3io.wp.backup.cli.parser import BACKUP_COMMAND, VERIFY_COMMAND, build_parser
from l3io.wp.backup.domain.errors import RunTerminated
from l3io.wp.backup.domain.exit_codes import ExitCode

__all__ = ["main", "run_from_cli"]


def _terminate(signum: int, frame: FrameType | None) -> None:
    del frame
    raise RunTerminated(signal.Signals(signum).name)


@contextmanager
def _sigterm_raises() -> Iterator[None]:
    """SIGTERM raises :class:`RunTerminated` inside the block, then is restored."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.signal(signal.SIGTERM, _terminate)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def main(argv: Sequence[str] | None = None) -> int:
    """Parse ``argv`` and dispatch. Returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.action is None:
        parser.print_help(sys.stderr)
        return int(ExitCode.USAGE)
    if args.action not in (BACKUP_COMMAND, VERIFY_COMMAND):
        parser.error(f"{args.action!r} is not implemented in this build")
    configure_logging(args.log_level, args.log_format)
    secrets = SecretRegistry()
    try:
        with _sigterm_raises():
            if args.action == BACKUP_COMMAND:
                from l3io.wp.backup.cli.backup import run_backup

                code = run_backup(args, secrets=secrets)
            else:
                from l3io.wp.backup.cli.verify import run_verify

                code = run_verify(args, secrets=secrets)
    except Exception as exc:
        # Every failure is mapped to its code; KeyboardInterrupt is not caught.
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
