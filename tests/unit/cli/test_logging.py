"""AD-10 — one logging configuration, one stream, one Run ID on every record."""

from __future__ import annotations

import json
import logging
import os
import pty
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
import structlog

from l3io.wp.backup.adapters.redaction import REDACTED, SecretRegistry, redact_open_runs
from l3io.wp.backup.cli.context import open_run
from l3io.wp.backup.cli.logging import configure_logging, processor_chain
from l3io.wp.backup.cli.parser import build_parser

SECRET = "sentinel-logging-secret"


def _lines(err: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in err.splitlines() if line.strip()]


@pytest.mark.parametrize("fmt", ["console", "json"])
def test_the_chain_order(fmt: str) -> None:
    def trace(_l: object, _m: str, event_dict: dict[str, object]) -> object:
        return event_dict

    chain = processor_chain(fmt, trace_processors=(trace,))  # type: ignore[arg-type]
    assert chain[0] is structlog.contextvars.merge_contextvars
    exc = chain.index(structlog.processors.format_exc_info)
    assert chain[exc + 1] is trace
    assert chain[exc + 2] is redact_open_runs
    assert chain.index(redact_open_runs) == len(chain) - 2
    renderer = (
        structlog.processors.JSONRenderer
        if fmt == "json"
        else structlog.dev.ConsoleRenderer
    )
    assert isinstance(chain[-1], renderer)


def test_with_no_trace_processors_redaction_is_still_second_to_last() -> None:
    chain = processor_chain("json")
    assert chain.index(redact_open_runs) == len(chain) - 2
    assert chain[-3] is structlog.processors.format_exc_info


def test_the_log_level_default_is_info_read_from_the_parser() -> None:
    args = build_parser().parse_args(["verify", "--archive", "x"])
    assert args.log_level == "INFO"
    assert args.log_format is None


@pytest.mark.parametrize("spelling", ["debug", "Warning", "ERROR"])
def test_the_log_level_is_case_insensitive(spelling: str) -> None:
    argv = ["--log-level", spelling, "verify", "--archive", "x"]
    args = build_parser().parse_args(argv)
    assert args.log_level == spelling.upper()


@pytest.mark.parametrize(
    "argv",
    [["--log-level", "LOUD"], ["--log-level", "NOTSET"], ["--log-format", "xml"]],
)
def test_an_invalid_log_flag_is_a_usage_error(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        build_parser().parse_args([*argv, "verify", "--archive", "x"])
    assert caught.value.code == 2


def test_timestamps_are_utc_and_end_in_z(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "json")
    structlog.get_logger("t").info("tick")
    (line,) = _lines(capsys.readouterr().err)
    stamp = str(line["timestamp"])
    assert stamp.endswith("Z")
    parsed = datetime.fromisoformat(stamp)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == UTC.utcoffset(parsed)
    assert line["level"] == "info"
    assert line["logger"] == "t"


def test_json_when_stderr_is_not_a_tty(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", None)
    structlog.get_logger("t").info("tick")
    assert _lines(capsys.readouterr().err)[0]["event"] == "tick"


def test_console_when_stderr_is_a_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    primary, secondary = pty.openpty()
    with os.fdopen(secondary, "w") as terminal:
        monkeypatch.setattr(sys, "stderr", terminal)
        configure_logging("INFO", None)
        structlog.get_logger("t").info("tick", answer=42)
        terminal.flush()
        text = os.read(primary, 65536).decode()
    os.close(primary)
    assert "tick" in text
    assert "answer" in text
    assert not text.lstrip().startswith("{")


def test_reconfiguring_replaces_the_handler(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "json")
    configure_logging("INFO", "json")
    ours = [h for h in logging.getLogger().handlers if h.get_name() == "l3io.wp.backup"]
    assert len(ours) == 1
    structlog.get_logger("t").info("once")
    assert len(_lines(capsys.readouterr().err)) == 1


def test_the_level_filters(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("warning", "json")
    log = structlog.get_logger("t")
    log.info("hidden")
    log.warning("shown")
    assert [e["event"] for e in _lines(capsys.readouterr().err)] == ["shown"]


def test_stdlib_and_structlog_records_share_the_run_id_and_correlation_id(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """``l3io.wp.config`` logs through stdlib only (AD-2); it lands in the stream."""
    configure_logging("DEBUG", "json")
    structlog.contextvars.bind_contextvars(correlation_id="caller-7")
    try:
        with open_run(operation="backup", workspace_parent=tmp_path) as ctx:
            logging.getLogger("l3io.wp.config").info("read %s", "wp-config.php")
            structlog.get_logger("l3io.wp.database").info("dumped")
    finally:
        structlog.contextvars.clear_contextvars()
    lines = _lines(capsys.readouterr().err)
    assert {line["l3io_run_id"] for line in lines} == {ctx.run_id}
    assert {line["correlation_id"] for line in lines} == {"caller-7"}
    loggers = {line["logger"] for line in lines}
    assert {"l3io.wp.config", "l3io.wp.database"} <= loggers
    config = next(line for line in lines if line["logger"] == "l3io.wp.config")
    assert config["event"] == "read wp-config.php"
    assert str(config["timestamp"]).endswith("Z")


def test_the_run_registry_redacts_both_kinds_of_record(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    configure_logging("DEBUG", "json")
    registry = SecretRegistry()
    registry.register(SECRET, source="test")
    with open_run(operation="backup", secrets=registry, workspace_parent=tmp_path):
        logging.getLogger("l3io.wp.config").warning("value %s", SECRET)
        structlog.get_logger("x").debug("planted", nested={"k": [SECRET]})
        try:
            raise ValueError(SECRET)  # noqa: TRY301
        except ValueError:
            structlog.get_logger("x").exception("failed")
    err = capsys.readouterr().err
    assert SECRET not in err
    assert err.count(REDACTED) >= 3


def test_a_record_from_a_thread_without_the_run_context_is_still_redacted(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    import threading

    configure_logging("INFO", "json")
    registry = SecretRegistry()
    registry.register(SECRET, source="test")
    with open_run(operation="backup", secrets=registry, workspace_parent=tmp_path):
        worker = threading.Thread(
            target=lambda: logging.getLogger("drainer").warning("saw %s", SECRET)
        )
        worker.start()
        worker.join()
    err = capsys.readouterr().err
    assert SECRET not in err
    (line,) = [e for e in _lines(err) if e["logger"] == "drainer"]
    assert "run_id" not in line  # documented: a raw thread does not inherit it
