"""The argparse composition root.

Configures logging once (AD-10, ``cli/logging.py``) before any Run opens,
then dispatches: ``backup`` to :func:`l3io.wp.backup.cli.backup.run_backup`
and ``verify`` to :func:`l3io.wp.backup.cli.verify.run_verify`, whose drivers
open the Run Context (AD-16). The exit-code table is E002-S01-005; anything
else is refused rather than pretending to act.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence

from l3io.wp.backup.cli.logging import configure_logging
from l3io.wp.backup.cli.parser import BACKUP_COMMAND, VERIFY_COMMAND, build_parser

__all__ = ["main", "run_from_cli"]


def main(argv: Sequence[str] | None = None) -> int:
    """Parse ``argv`` and dispatch. Returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.action is None:
        parser.print_help(sys.stderr)
        return 2
    configure_logging(args.log_level, args.log_format)
    if args.action == BACKUP_COMMAND:
        from l3io.wp.backup.cli.backup import run_backup

        return run_backup(args)
    if args.action == VERIFY_COMMAND:
        from l3io.wp.backup.cli.verify import run_verify

        return run_verify(args)
    parser.error(f"{args.action!r} is not implemented in this build")


def run_from_cli() -> None:
    """Console-script entry point.

    A closed stdout (``| head``) does not mask the Run's exit code: the
    ``BrokenPipeError`` is caught and stdout pointed at ``/dev/null`` so the
    interpreter's own flush at exit cannot raise again.
    """
    code = 1
    try:
        code = main()
        sys.stdout.flush()
    except BrokenPipeError:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    raise SystemExit(code)
