"""FR-27/AD-13 (E008-S01-001, moved from E005-S01-002) -- a real CLI Run refused.

``wpbackup2 backup --encrypt-with-passphrase --mode files`` under
``L3IO_AGE_PASSPHRASE_BACKEND=cli``, through ``cli.main``, with ``PATH`` set to
one absolute directory the test controls:

* a fake ``age`` printing ``age v1.2.0`` (and a fake ``age-plugin-batchpass``):
  the cipher's ``check()`` passes (the tools are present), then the root's
  ``RootPreflight`` routes ``age`` to ``BinaryPreflight``, which reads 1.2.0,
  and the Run exits ``NOT_READY`` (7) before the first command adapter. The
  failure names ``age`` and the step; the ``preflight_checked`` record carries
  the minimum ``1.3``. The Storage Target is unchanged and no workspace is
  left behind.
* no ``age`` at all: the cipher's ``check()`` refuses first, with
  ``EncryptionConfigError``, exit ``USAGE`` (2), before any Run opens.

A files-only Run touches no database, so this needs no server; it needs the
``database`` extra (the cipher is database's). The passphrase is a unique
sentinel in ``L3IO_WP_BACKUP_PASSPHRASE`` and is never printed.
"""

from __future__ import annotations

import json
import tempfile
import uuid
from pathlib import Path

import pytest

pytest.importorskip("l3io.wp.database")

from l3io.wp.backup.adapters.workspace import WORKSPACE_PREFIX
from l3io.wp.backup.cli import main
from l3io.wp.backup.cli.passphrase import PASSPHRASE_ENV
from l3io.wp.backup.domain.exit_codes import ExitCode

pytestmark = pytest.mark.integration

STEP = "ENCRYPT"


def _script(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path | str]:
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.php").write_text("<?php\n", encoding="utf-8")
    (site / "wp-config.php").write_text(
        "<?php\n"
        "define('DB_NAME', 'e008_preflight');\n"
        "define('DB_USER', 'e008_user');\n"
        f"define('DB_PASSWORD', 'pw-{uuid.uuid4().hex}');\n"
        "define('DB_HOST', 'db.invalid');\n"
        "$table_prefix = 'wp_';\n",
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    target = tmp_path / "out"
    target.mkdir()
    work = tmp_path / "tmp"
    work.mkdir()
    passphrase = f"e008-preflight-pp-{uuid.uuid4().hex}"
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("L3IO_AGE_PASSPHRASE_BACKEND", "cli")
    monkeypatch.setenv(PASSPHRASE_ENV, passphrase)
    monkeypatch.setenv("TMPDIR", str(work))
    monkeypatch.setattr(tempfile, "tempdir", None)
    return {
        "site": site,
        "bin": bin_dir,
        "target": target,
        "work": work,
        "passphrase": passphrase,
    }


def _backup(
    run: dict[str, Path | str], capsys: pytest.CaptureFixture[str]
) -> tuple[int, str, list[dict[str, object]]]:
    capsys.readouterr()
    code = main(
        [
            "--log-format",
            "json",
            "backup",
            "--wp-dir",
            str(run["site"]),
            "--archive",
            str(Path(run["target"]) / "site.tar.gz"),
            "--mode",
            "files",
            "--encrypt-with-passphrase",
        ]
    )
    captured = capsys.readouterr()
    records = [
        json.loads(line) for line in captured.err.splitlines() if line.startswith("{")
    ]
    assert str(run["passphrase"]) not in captured.out + captured.err
    return code, captured.err, records


def _nothing_left(run: dict[str, Path | str]) -> None:
    assert list(Path(run["target"]).iterdir()) == [], "the Storage Target changed"
    assert list(Path(run["work"]).glob(f"{WORKSPACE_PREFIX}*")) == [], (
        "a workspace remains"
    )


def test_an_age_below_1_3_refuses_the_cli_run_before_any_effect(
    run: dict[str, Path | str], capsys: pytest.CaptureFixture[str]
) -> None:
    bin_dir = Path(run["bin"])
    marker = bin_dir.parent / "age-ran"
    _script(bin_dir, "age", f"echo ran >> '{marker}'\necho 'age v1.2.0'")
    _script(bin_dir, "age-plugin-batchpass", "exit 0")
    code, err, records = _backup(run, capsys)
    assert code == int(ExitCode.NOT_READY) == 7, err[-3000:]
    (failed,) = [r for r in records if r.get("event") == "command_failed"]
    assert failed["error"] == "PreflightFailedError"
    message = str(failed["message"])
    assert "age" in message
    assert STEP in message
    assert "1.2.0" in message
    checked = [
        r
        for r in records
        if r.get("event") == "preflight_checked" and r.get("tool") == "age"
    ]
    assert [r["minimum"] for r in checked] == ["1.3"]
    assert marker.exists(), "the probe never ran the fake age"
    assert not [r for r in records if r.get("event") == "phase_started"], (
        "a command phase started before preflight refused"
    )
    _nothing_left(run)


def test_with_no_age_the_cipher_check_refuses_first(
    run: dict[str, Path | str], capsys: pytest.CaptureFixture[str]
) -> None:
    _script(Path(run["bin"]), "age-plugin-batchpass", "exit 0")
    code, err, records = _backup(run, capsys)
    assert code == int(ExitCode.USAGE) == 2, err[-3000:]
    (failed,) = [r for r in records if r.get("event") == "command_failed"]
    assert failed["error"] == "EncryptionConfigError"
    assert not [r for r in records if r.get("event") == "run_started"], (
        "a Run opened before the cipher refused"
    )
    _nothing_left(run)
