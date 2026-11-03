"""FR-20/AD-10 — one Run ID on every line a real backup writes to stderr.

A full ``wpbackup2 --log-format json --log-level DEBUG backup`` against the CI
database, stderr captured as written. Every line parses as JSON and carries the
Run's ``run_id`` and the caller's pre-bound ``correlation_id``; the first record
is ``run_started`` and the last ``run_finished``; each plan step has one
``phase_completed`` with ``phase``, ``duration_ms``, ``exit_code`` and
``bytes``. Records from ``l3io.wp.database`` (structlog) and ``l3io.wp.config``
(stdlib only, AD-2) are asserted **present**, so the check cannot pass over an
empty set.

``l3io.wp.config`` logs only when asked to write (``WpConfigFile.set``, at
DEBUG), and a backup only reads, so the Run's descriptor step is wrapped to
make one real, no-change ``set`` on a scratch copy of the config — real
``l3io.wp.config`` code emitting a real stdlib record inside the Run.

Needs the CI ``db`` service; skips when ``DB_HOST`` is unset.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
import structlog

pytest.importorskip("l3io.wp.database")
wpconfig = pytest.importorskip("l3io.wp.config")

from l3io.wp.backup.cli import backup as backup_cli  # noqa: E402
from l3io.wp.backup.cli import main  # noqa: E402
from l3io.wp.backup.core.backup import BackupStep  # noqa: E402
from tests.integration.test_backup_end_to_end import DB_NAME, site  # noqa: E402, F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        "DB_HOST" not in os.environ, reason="needs the CI database (DB_HOST unset)"
    ),
]

PHASE_FIELDS = frozenset({"phase", "duration_ms", "exit_code", "bytes"})


def test_every_line_of_a_backup_carries_one_run_id(
    tmp_path: Path,
    site: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    real = backup_cli.site_descriptor_from_wp_config
    scratch = tmp_path / "scratch-wp-config.php"

    def descriptor(site_root: Path, *, config_file: Path) -> object:
        shutil.copyfile(config_file, scratch)
        wpconfig.WpConfigFile(scratch).set("DB_NAME", DB_NAME)
        return real(site_root, config_file=config_file)

    monkeypatch.setattr(backup_cli, "site_descriptor_from_wp_config", descriptor)
    final = tmp_path / "site.tar.gz"
    structlog.contextvars.bind_contextvars(correlation_id="caller-correlation")
    try:
        code = main(
            ["--log-format", "json", "--log-level", "DEBUG", "backup",
             "--wp-dir", str(site), "--archive", str(final)]
        )  # fmt: skip
    finally:
        structlog.contextvars.clear_contextvars()
    err = capsys.readouterr().err
    assert code == 0, err
    lines: list[dict[str, Any]] = [json.loads(line) for line in err.splitlines()]
    assert lines, "nothing was written to stderr"

    run_ids = {line.get("run_id") for line in lines}
    assert len(run_ids) == 1
    assert None not in run_ids
    assert {line.get("correlation_id") for line in lines} == {"caller-correlation"}
    assert lines[0]["event"] == "run_started"
    assert lines[-1]["event"] == "run_finished"
    assert lines[-1]["exit_code"] == 0

    loggers = {str(line["logger"]) for line in lines}
    assert any(name.startswith("l3io.wp.database") for name in loggers), loggers
    assert any(name.startswith("l3io.wp.config") for name in loggers), loggers

    phases = [line for line in lines if line["event"] == "phase_completed"]
    steps = [s.value for s in BackupStep if s is not BackupStep.ENCRYPT]
    assert [p["phase"] for p in phases] == steps
    assert all(p.keys() >= PHASE_FIELDS for p in phases)
    assert all(str(line["timestamp"]).endswith("Z") for line in lines)
