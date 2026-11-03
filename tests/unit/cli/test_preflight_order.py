"""AD-13 — preflight completes before the first command adapter is called.

A recording ``Preflight`` and recording command adapters share one event log.
In a real Run and in a Dry Run alike, ``preflight`` precedes every command
call; a failing preflight raises ``PreflightFailedError`` and leaves the
command log empty. The recorded command set is every ``AdapterSet`` field
whose resolved type holds a ``*Command`` Protocol found under ``ports/``
(``tests/port_roles.py``), so a command added later is recorded too.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from l3io.wp.backup.adapters.database import (
    locate_wp_config,
    site_descriptor_from_wp_config,
)
from l3io.wp.backup.cli import backup as backup_cli
from l3io.wp.backup.cli.adapter_sets import dry_run_adapters, real_adapters
from l3io.wp.backup.cli.context import RunContext, open_run
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.errors import PreflightFailedError
from l3io.wp.backup.domain.exclusion import INCLUDE_EVERYTHING
from l3io.wp.backup.domain.preflight import ToolRequirement, ToolStatus
from l3io.wp.backup.ports.adapter_set import AdapterSet
from tests.port_roles import command_fields

pytestmark = pytest.mark.usefixtures("fake_database")

COMMAND_FIELDS = tuple(command_fields(AdapterSet))


class Recording:
    """Delegates to ``inner``, logging each method call as ``label.method``."""

    def __init__(self, label: str, inner: object, events: list[str]) -> None:
        self._label, self._inner, self._events = label, inner, events

    def __getattr__(self, name: str) -> object:
        value = getattr(self._inner, name)
        if not callable(value):
            return value

        def call(*args: object, **kwargs: object) -> object:
            self._events.append(f"{self._label}.{name}")
            return value(*args, **kwargs)

        return call


class RecordingPreflight:
    def __init__(self, events: list[str], *, present: bool = True) -> None:
        self.events, self.present = events, present

    def check(self, requirements: Sequence[ToolRequirement]) -> Sequence[ToolStatus]:
        self.events.append("preflight")
        return [
            ToolStatus(name=r.name, present=self.present, version=None, path=None)
            for r in requirements
        ]


def _recorded(adapter_set: AdapterSet, events: list[str]) -> AdapterSet:
    return dataclasses.replace(
        adapter_set,
        **{
            name: Recording(name, getattr(adapter_set, name), events)
            for name in COMMAND_FIELDS
        },
    )


@pytest.fixture
def run(workdir: Path) -> Iterator[RunContext]:
    with open_run(operation="backup") as ctx:
        yield ctx


def _real(ctx: RunContext) -> AdapterSet:
    return real_adapters(
        wordpress_source=object(),  # type: ignore[arg-type]
        secrets=ctx.secrets,
        tracer=ctx.tracer,
    )


SELECTIONS: dict[str, Callable[[AdapterSet], AdapterSet]] = {
    "real": lambda real: real,
    "dry_run": dry_run_adapters,
}


def _backup(ctx: RunContext, site: Path, final: Path, preflight: object) -> object:
    config = locate_wp_config(site, stated=None)
    descriptor, _ = site_descriptor_from_wp_config(site, config_file=config.path)
    with capture_logs():
        return backup_cli.backup(
            descriptor,
            BackupScope.ALL,
            backup_cli.storage_target_from_cli(str(final)),
            ctx=ctx,
            config=config,
            cipher=None,
            exclusion=INCLUDE_EVERYTHING,
            origin=None,
            preflight=preflight,  # type: ignore[arg-type]
        )


def test_the_recorded_commands_are_every_command_field() -> None:
    assert set(COMMAND_FIELDS) == {
        "archive_store_command",
        "database_command",
        "archive_writer",
        "restore_database_command",
        "site_files_command",
        "config_command",
        "search_replace",
        "journal_command",
    }


@pytest.mark.parametrize("selection", sorted(SELECTIONS))
def test_preflight_completes_before_the_first_command(
    run: RunContext, site: Path, tmp_path: Path, selection: str
) -> None:
    events: list[str] = []
    run.bind_adapters(_recorded(SELECTIONS[selection](_real(run)), events))
    _backup(run, site, tmp_path / "a.tar.gz", RecordingPreflight(events))
    assert events[0] == "preflight"
    assert events.count("preflight") == 1
    assert len(events) > 1, "no command was called"
    assert (tmp_path / "a.tar.gz").exists() is (selection == "real")


@pytest.mark.parametrize("selection", sorted(SELECTIONS))
def test_a_failing_preflight_leaves_the_command_log_empty(
    run: RunContext, site: Path, tmp_path: Path, selection: str
) -> None:
    events: list[str] = []
    run.bind_adapters(_recorded(SELECTIONS[selection](_real(run)), events))
    with pytest.raises(PreflightFailedError) as caught:
        _backup(
            run, site, tmp_path / "a.tar.gz", RecordingPreflight(events, present=False)
        )
    assert events == ["preflight"]
    assert caught.value.found == "absent"
    assert not (tmp_path / "a.tar.gz").exists()
