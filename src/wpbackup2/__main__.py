"""``python -m wpbackup2`` — the console entry ``wpbackup2``, nothing else (AD-22)."""

from l3io.wp.backup.cli import run_from_cli

if __name__ == "__main__":
    # The console script's own call: `run_from_cli` raises the Run's
    # SystemExit itself (it is typed NoReturn), so both invocations exit alike.
    run_from_cli()
