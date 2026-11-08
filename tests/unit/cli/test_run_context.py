"""AD-16/ADR-0016 — one Run Context per Run: created once, joined, never duplicated."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import structlog
from structlog.testing import capture_logs

from l3io.wp.backup.adapters import redaction
from l3io.wp.backup.adapters.phase import ACTIVE_TRACER
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.adapters.workspace import WORKSPACE_PREFIX
from l3io.wp.backup.cli.context import RunContext, active_run, join_or_open, open_run
from l3io.wp.backup.domain.errors import (
    AdaptersAlreadyBoundError,
    AdaptersNotBoundError,
    BackupError,
    RefusedError,
    RunContextActiveError,
)


class Boom(BackupError):
    pass


def _events(logs: list[dict[str, object]], name: str) -> list[dict[str, object]]:
    return [e for e in logs if e["event"] == name]


def _adapter_set() -> object:
    return object()


def test_one_run_id_per_run_and_it_is_a_uuid4(workdir: Path) -> None:
    with capture_logs() as logs, open_run(operation="backup") as ctx:
        assert active_run() is ctx
    assert uuid.UUID(ctx.run_id).version == 4
    assert str(uuid.UUID(ctx.run_id)) == ctx.run_id
    assert ctx.started_at.utcoffset() is not None
    assert ctx.started_at.utcoffset().total_seconds() == 0  # type: ignore[union-attr]
    started, finished = _events(logs, "run_started"), _events(logs, "run_finished")
    assert [e["l3io_run_id"] for e in started + finished] == [ctx.run_id] * 2
    assert started[0]["operation"] == "backup"
    assert str(started[0]["started_at"]).endswith("Z")
    assert finished[0]["exit_code"] == 0
    assert active_run() is None
    assert redaction._OPEN == []  # noqa: SLF001 - the registry is released
    assert ACTIVE_TRACER.get() is None


def test_two_runs_get_two_ids(workdir: Path) -> None:
    with open_run(operation="backup") as first:
        pass
    with open_run(operation="backup") as second:
        pass
    assert first.run_id != second.run_id


def test_the_run_id_is_bound_and_only_what_was_bound_is_unbound(workdir: Path) -> None:
    structlog.contextvars.bind_contextvars(correlation_id="caller-1")
    try:
        with open_run(operation="backup") as ctx:
            bound = structlog.contextvars.get_contextvars()
            assert bound == {
                "correlation_id": "caller-1",
                "l3io_run_id": ctx.run_id,
                "l3io_dry_run": False,
            }
        assert structlog.contextvars.get_contextvars() == {"correlation_id": "caller-1"}
    finally:
        structlog.contextvars.clear_contextvars()


def test_a_nested_open_run_is_refused_naming_the_active_run(workdir: Path) -> None:
    with open_run(operation="backup") as ctx:
        with pytest.raises(RunContextActiveError) as caught:  # noqa: SIM117
            with open_run(operation="verify"):
                pass
        assert isinstance(caught.value, RefusedError)
        assert ctx.run_id in str(caught.value)
        assert active_run() is ctx


def test_join_or_open_with_a_context_creates_nothing(workdir: Path) -> None:
    with open_run(operation="backup") as ctx:
        before = sorted(workdir.iterdir())
        with capture_logs() as logs, join_or_open(ctx, operation="verify") as handle:
            assert handle.ctx is ctx
            assert handle.opened is False
            assert sorted(workdir.iterdir()) == before
        assert _events(logs, "run_started") == []
        assert _events(logs, "run_finished") == []
        assert active_run() is ctx


def test_join_or_open_without_a_context_opens_one(workdir: Path) -> None:
    registry = SecretRegistry()
    with join_or_open(None, operation="verify", secrets=registry) as handle:
        assert handle.opened is True
        assert handle.ctx.secrets is registry
        assert active_run() is handle.ctx


def test_the_workspace_is_private_and_removed_on_success(workdir: Path) -> None:
    with open_run(operation="backup") as ctx:
        assert ctx.workspace.parent == workdir
        assert ctx.workspace.name.startswith(WORKSPACE_PREFIX)
        assert ctx.workspace.stat().st_mode & 0o777 == 0o700
    assert not ctx.workspace.exists()


def test_the_workspace_parent_can_be_given(tmp_path: Path, workdir: Path) -> None:
    parent = tmp_path / "elsewhere"
    parent.mkdir()
    with open_run(operation="backup", workspace_parent=parent) as ctx:
        assert ctx.workspace.parent == parent


@pytest.mark.parametrize("raised", [Boom("boom"), KeyboardInterrupt()])
def test_a_failure_still_finishes_the_run_and_removes_the_workspace(
    workdir: Path, raised: BaseException
) -> None:
    seen: list[RunContext] = []

    def run() -> None:
        with open_run(operation="backup") as ctx:
            seen.append(ctx)
            raise raised

    with capture_logs() as logs, pytest.raises(type(raised)):
        run()
    (finished,) = _events(logs, "run_finished")
    # BL-E002-056: an interrupt exits with the interpreter's status (130),
    # which is not the table's, so no code is claimed for it.
    claimed = isinstance(raised, Exception)
    assert finished["exit_code"] == (1 if claimed else None)
    assert finished["exit_reason"] == ("FAILED" if claimed else None)
    assert finished["error"] == type(raised).__name__
    assert finished["log_level"] == "error"
    assert not seen[0].workspace.exists()
    assert active_run() is None
    assert structlog.contextvars.get_contextvars() == {}
    assert redaction._OPEN == []  # noqa: SLF001 - the registry is released
    assert ACTIVE_TRACER.get() is None


def test_run_finished_is_the_last_record_of_the_run(workdir: Path) -> None:
    with capture_logs() as logs, open_run(operation="backup"):
        structlog.get_logger("inside").info("work")
    assert logs[0]["event"] == "run_started"
    assert logs[-1]["event"] == "run_finished"


def test_reading_adapters_before_a_bind_is_a_defect(workdir: Path) -> None:
    with open_run(operation="backup") as ctx:
        with pytest.raises(AdaptersNotBoundError) as caught:
            _ = ctx.adapters
        assert not isinstance(caught.value, RefusedError)


def test_adapters_bind_exactly_once(workdir: Path) -> None:
    with open_run(operation="backup") as ctx:
        first = _adapter_set()
        ctx.bind_adapters(first)  # type: ignore[arg-type]
        assert ctx.adapters is first
        with pytest.raises(AdaptersAlreadyBoundError):
            ctx.bind_adapters(_adapter_set())  # type: ignore[arg-type]
        assert ctx.adapters is first


def test_the_repr_names_neither_registry_nor_workspace(workdir: Path) -> None:
    with open_run(operation="backup") as ctx:
        text = repr(ctx)
    assert ctx.run_id in text
    assert str(ctx.workspace) not in text
    assert "SecretRegistry" not in text


def test_command_failed_carries_the_failed_runs_id(workdir: Path) -> None:
    from l3io.wp.backup.cli.exit import report_failure

    secrets = SecretRegistry()
    with capture_logs() as logs:
        message = "nope"
        with (
            pytest.raises(Boom) as raised,
            open_run(operation="backup", secrets=secrets),
        ):
            raise Boom(message)
        report_failure(raised.value, secrets=secrets)
    (finished,) = _events(logs, "run_finished")
    (failed,) = _events(logs, "command_failed")
    assert failed["l3io_run_id"] == finished["l3io_run_id"]
