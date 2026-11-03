"""The argparse composition root.

Binds adapters, creates the Run Context (AD-16), configures ``structlog``
(AD-10) and translates errors to the exit-code table (FR-21). ``backup``
dispatches to :func:`l3io.wp.backup.cli.backup.run_backup`; the Run Context,
logging configuration and exit-code table are later stories, and anything
else is refused rather than pretending to act.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from l3io.wp.backup.cli.parser import BACKUP_COMMAND, build_parser

__all__ = ["main", "run_from_cli"]


def main(argv: Sequence[str] | None = None) -> int:
    """Parse ``argv`` and dispatch. Returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.action is None:
        parser.print_help(sys.stderr)
        return 2
    if args.action == BACKUP_COMMAND:
        from l3io.wp.backup.cli.backup import run_backup

        return run_backup(args)
    parser.error(f"{args.action!r} is not implemented in this build")


def run_from_cli() -> None:
    """Console-script entry point."""
    raise SystemExit(main())
