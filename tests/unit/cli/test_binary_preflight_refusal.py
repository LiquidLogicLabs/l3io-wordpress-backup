"""FR-27/AD-13 — ``BinaryPreflight`` refuses a Run before its first effect.

Drives the built ``cli/backup.py::backup(..., preflight=)`` seam inside a Run
opened by ``open_run``, with ``BinaryPreflight(cwd=ctx.workspace)`` bound and an
``age`` requirement handed in through ``extra_tools`` — the way a cipher's
``required_tools()`` arrives. ``PATH`` holds only a directory the test controls:
a fake ``age`` printing ``age v1.2.0``, or nothing. The Run must raise
``PreflightFailedError`` (exit 7) with no command adapter called, the Storage
Target untouched and no workspace left behind.

Binding ``BinaryPreflight`` into every root (``RootPreflight``) and the real
passphrase-encrypted CLI Run are E008-S01-001's.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from l3io.wp.backup.adapters.database import (
    locate_wp_config,
    site_descriptor_from_wp_config,
)
from l3io.wp.backup.adapters.preflight import BinaryPreflight
from l3io.wp.backup.adapters.workspace import WORKSPACE_PREFIX
from l3io.wp.backup.cli import backup as backup_cli
from l3io.wp.backup.cli.adapter_sets import real_adapters
from l3io.wp.backup.cli.context import open_run
from l3io.wp.backup.cli.exit import exit_code_for
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.errors import PreflightFailedError
from l3io.wp.backup.domain.exclusion import INCLUDE_EVERYTHING
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.domain.preflight import ToolRequirement
from l3io.wp.backup.ports.adapter_set import AdapterSet
from tests.port_roles import command_fields

pytestmark = pytest.mark.usefixtures("fake_database")

STEP = "ENCRYPT (passphrase, CLI backend)"
AGE = ToolRequirement("age", (1, 3), STEP)


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


def _recorded(adapter_set: AdapterSet, events: list[str]) -> AdapterSet:
    return dataclasses.replace(
        adapter_set,
        **{
            name: Recording(name, getattr(adapter_set, name), events)
            for name in command_fields(AdapterSet)
        },
    )


@pytest.fixture
def path_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The only directory on ``PATH``; empty unless a case plants a tool."""
    directory = tmp_path / "bin"
    directory.mkdir()
    monkeypatch.setenv("PATH", str(directory))
    return directory


def _fake_age(directory: Path, version: str) -> None:
    age = directory / "age"
    age.write_text(f'#!/bin/sh\necho "age v{version}"\n', encoding="utf-8")
    age.chmod(0o755)


def _run(site: Path, final: Path) -> tuple[list[str], PreflightFailedError | None]:
    events: list[str] = []
    config = locate_wp_config(site, stated=None)
    descriptor, _ = site_descriptor_from_wp_config(site, config_file=config.path)
    try:
        with capture_logs(), open_run(operation="backup") as ctx:
            ctx.bind_adapters(
                _recorded(
                    real_adapters(
                        wordpress_source=object(),  # type: ignore[arg-type]
                        secrets=ctx.secrets,
                        tracer=ctx.tracer,
                    ),
                    events,
                )
            )
            backup_cli.backup(
                descriptor,
                BackupScope.FILES,
                backup_cli.storage_target_from_cli(str(final)),
                ctx=ctx,
                config=config,
                cipher=None,
                exclusion=INCLUDE_EVERYTHING,
                origin=None,
                extra_tools=(AGE,),
                preflight=BinaryPreflight(cwd=ctx.workspace),
            )
    except PreflightFailedError as failed:
        return events, failed
    return events, None


def _assert_refused_before_any_effect(
    events: list[str], failed: PreflightFailedError | None, target: Path, work: Path
) -> PreflightFailedError:
    assert failed is not None, "the Run was not refused"
    assert exit_code_for(failed) is ExitCode.NOT_READY
    assert int(ExitCode.NOT_READY) == 7
    assert failed.tool == "age"
    assert failed.needed_for == STEP
    assert "age" in str(failed)
    assert STEP in str(failed)
    assert events == [], f"a command adapter was called: {events}"
    assert list(target.iterdir()) == [], "the Storage Target directory changed"
    assert list(work.glob(f"{WORKSPACE_PREFIX}*")) == [], "a workspace remains"
    return failed


def test_an_age_below_the_minimum_refuses_the_run(
    site: Path, workdir: Path, tmp_path: Path, path_dir: Path
) -> None:
    _fake_age(path_dir, "1.2.0")
    target = tmp_path / "out"
    target.mkdir()
    events, failed = _run(site, target / "a.tar.gz")
    refused = _assert_refused_before_any_effect(events, failed, target, workdir)
    assert refused.found == "1.2.0"


def test_an_absent_age_refuses_the_run(
    site: Path, workdir: Path, tmp_path: Path, path_dir: Path
) -> None:
    target = tmp_path / "out"
    target.mkdir()
    events, failed = _run(site, target / "a.tar.gz")
    refused = _assert_refused_before_any_effect(events, failed, target, workdir)
    assert refused.found == "absent"


def test_an_age_at_the_minimum_lets_the_run_through(
    site: Path, tmp_path: Path, path_dir: Path
) -> None:
    """The control: the same Run with ``age`` 1.3.0 writes its Archive."""
    _fake_age(path_dir, "1.3.0")
    target = tmp_path / "out"
    target.mkdir()
    events, failed = _run(site, target / "a.tar.gz")
    assert failed is None
    assert events, "no command adapter was called"
    assert (target / "a.tar.gz").exists()
