"""``python -m wpbackup2`` — the console entry ``wpbackup2``, nothing else (AD-22)."""

from l3io.wp.backup.cli import run_from_cli

if __name__ == "__main__":
    # The same call the generated console script makes, `sys.exit(entry())`, so
    # the two invocations exit alike. `run_from_cli` raises its own SystemExit
    # today, hence the ignore; the wrapper keeps parity if it ever returns.
    raise SystemExit(run_from_cli())  # type: ignore[func-returns-value]
