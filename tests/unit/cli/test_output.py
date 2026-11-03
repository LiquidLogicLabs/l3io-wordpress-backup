"""The CLI's result and failure output (``cli/output.py``).

A failed command leaves exactly one ``error`` record naming its class — before
a Run opened, too — and with ``--log-format json`` stderr stays one JSON object
per line, failures included, so ``jq`` over a log file is sufficient. A reader
that went away does not change the exit code.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from l3io.wp.backup.cli import main, run_from_cli
from l3io.wp.backup.domain import errors as error_classes
from l3io.wp.backup.domain.exit_codes import ExitCode

pytestmark = pytest.mark.usefixtures("fake_database")


def _json_lines(err: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in err.splitlines()]


@pytest.mark.parametrize(
    ("argv", "error"),
    [
        (["backup", "--wp-dir", "{missing}"], "InvalidSiteDescriptorError"),
        (["backup", "--wp-dir", "{site}", "--archive", "s3://h/x"], "UsageError"),
        (["verify", "--archive", "{missing}"], "ArchiveNotFoundError"),
    ],
)
def test_a_failure_is_one_json_error_record(
    argv: list[str],
    error: str,
    site: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    filled = [a.format(missing=tmp_path / "missing", site=site) for a in argv]
    code = getattr(error_classes, error).exit_code
    assert code is not ExitCode.OK
    assert main(["--log-format", "json", *filled]) == code
    lines = _json_lines(capsys.readouterr().err)
    # A Run's own failure records (phase_completed, run_finished) aside.
    own = {"run_finished", "phase_completed"}
    errors = [e for e in lines if e["level"] == "error" and e["event"] not in own]
    assert [(e["event"], e["error"]) for e in errors] == [("command_failed", error)]
    assert str(errors[0]["message"]).startswith("wpbackup2: error: ")


def test_a_failed_verify_is_json_too(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    corrupt = tmp_path / "corrupt.tar.gz"
    corrupt.write_bytes(b"x" * 2048)
    code = ExitCode.VERIFICATION_FAILED
    assert main(["--log-format", "json", "verify", "--archive", str(corrupt)]) == code
    lines = _json_lines(capsys.readouterr().err)
    (failed,) = [e for e in lines if e["event"] == "command_failed"]
    assert failed["level"] == "error"
    assert failed["error"] == "VerificationFailedError"
    assert (failed["exit_code"], failed["exit_reason"]) == (int(code), code.name)
    finished = next(e for e in lines if e["event"] == "run_finished")
    assert (finished["exit_code"], finished["exit_reason"]) == (int(code), code.name)
    (verified,) = [e for e in lines if e["event"] == "phase_completed"]
    assert verified["phase"] == "VERIFY"
    assert (verified["outcome"], verified["exit_code"]) == ("failed", int(code))


def test_the_console_format_also_prints_the_plain_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "missing"
    code = main(["--log-format", "console", "backup", "--wp-dir", str(missing)])
    assert code == ExitCode.FAILED
    err = capsys.readouterr().err
    assert any(line.startswith("wpbackup2: error: ") for line in err.splitlines())
    assert "command_failed" in err


def test_a_closed_stdout_does_not_change_a_successful_exit_code(
    site: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sink = (tmp_path / "sink").open("w")

    class ClosedPipe(io.TextIOWrapper):
        def write(self, text: str) -> int:
            raise BrokenPipeError

    pipe = ClosedPipe(io.BytesIO())
    monkeypatch.setattr(pipe, "fileno", sink.fileno)
    final = tmp_path / "out.tar.gz"
    argv = ["wpbackup2", "backup", "--wp-dir", str(site), "--archive", str(final)]
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(sys, "stdout", pipe)
    with pytest.raises(SystemExit) as caught:
        run_from_cli()
    sink.close()
    assert caught.value.code == 0
    assert final.is_file()
    capsys.readouterr()
