"""FR-19/AD-4 — ``wpbackup2 --whatIf backup`` against the CI database.

A Dry Run binds the real queries and the no-op commands: credentials resolve,
preflight and ``check_dump`` run against the live server, and the Storage
Target, the Site Root and every table are unchanged afterwards. With wrong
credentials it exits ``CREDENTIALS``, as the real Run does.

Needs the CI ``db`` service through ``DB_HOST``/``DB_PASSWORD``; skips when
``DB_HOST`` is unset. The fixtures are ``test_backup_end_to_end.py``'s.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("l3io.wp.database")
pytest.importorskip("pymysql")

from pymysql.connections import Connection

from l3io.wp.backup.cli import main
from l3io.wp.backup.domain.exit_codes import ExitCode
from tests.integration.test_backup_end_to_end import (
    _checksums,
    _snapshot,
    database,
    site,
)

__all__ = ["database", "site"]  # fixtures, re-exported for pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        "DB_HOST" not in os.environ, reason="needs the CI database (DB_HOST unset)"
    ),
]


def _listing(directory: Path) -> dict[str, tuple[int, int]]:
    """The Storage Target directory: names, sizes and mtimes."""
    return {
        path.name: (path.stat().st_size, path.stat().st_mtime_ns)
        for path in sorted(directory.iterdir())
    }


def test_a_dry_run_changes_nothing_and_prints_the_plan_in_order(
    tmp_path: Path,
    site: Path,
    database: Connection,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "earlier.tar.gz").write_bytes(b"an earlier archive")
    final = out / "site.tar.gz"
    listing, files, tables = _listing(out), _snapshot(site), _checksums(database)

    argv = ["--whatIf", "backup", "--wp-dir", str(site), "--archive", str(final)]
    code = main(argv)

    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert _listing(out) == listing
    assert _snapshot(site) == files
    assert _checksums(database) == tables
    lines = captured.out.splitlines()
    assert [line.split(".", 1)[0] for line in lines] == [
        str(n) for n in range(1, len(lines) + 1)
    ]
    assert "check dump readiness" in lines[1]
    assert lines[-1].endswith(f"publish to file:{final}")


@pytest.mark.parametrize("flag", [None, "--whatIf"])
def test_wrong_credentials_exit_credentials_in_a_run_and_a_dry_run(
    tmp_path: Path, site: Path, database: Connection, flag: str | None
) -> None:
    config = site / "wp-config.php"
    password = os.environ["DB_PASSWORD"]
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            f"'DB_PASSWORD', '{password}'", "'DB_PASSWORD', 'certainly-wrong'"
        ),
        encoding="utf-8",
    )
    final = tmp_path / "site.tar.gz"
    argv = ["backup", "--wp-dir", str(site), "--archive", str(final)]
    code = main(argv if flag is None else [flag, *argv])
    assert code == int(ExitCode.CREDENTIALS)
    assert not final.exists()
