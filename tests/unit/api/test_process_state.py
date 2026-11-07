"""AD-10 — a library call leaves the host's process state as it found it.

After every entry point returns or raises, ``structlog.is_configured()``, the
root logger's handlers and the ``SIGTERM`` handler equal their values before
the call. They are also read *during* every Run: inside every ``phase``
context, patched in each module that imported ``adapters.phase.phase`` (found
in ``sys.modules``, never listed), and inside the restore rig's executor, which
stands for the restore phases. So a handler installed and then restored
before the call returns is caught too, for each entry point.

The AST half: no module under ``api/`` (found on disk) calls
``signal.signal``/``signal.setitimer`` or a logging configurator, matched on
the name the call resolves to through the module's imports; planted calls in
every import spelling are reported.
"""

from __future__ import annotations

import ast
import logging
import signal
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import structlog

from l3io.wp.backup import api
from l3io.wp.backup.adapters import phase as phase_module
from l3io.wp.backup.api import operations
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.restore import ConfigPolicy, RestoreMode, SnapshotPolicy
from tests.qualified_names import ImportTable, module_package
from tests.restore_root_rig import ALL_WAIVED, Rig, report_for

pytestmark = pytest.mark.usefixtures("fake_database")

API = Path(operations.__file__).parent

State = tuple[bool, list[logging.Handler], object]


def _state() -> State:
    return (
        structlog.is_configured(),
        list(logging.getLogger().handlers),
        signal.getsignal(signal.SIGTERM),
    )


class During:
    """``(entry point, state)`` read inside the Run, by the entry point running."""

    def __init__(self) -> None:
        self.entry = "none"
        self.seen: list[tuple[str, State]] = []

    def sample(self) -> None:
        self.seen.append((self.entry, _state()))

    def entries(self) -> set[str]:
        return {entry for entry, _ in self.seen}


@pytest.fixture
def during(monkeypatch: pytest.MonkeyPatch, rig: Rig) -> During:
    """Sample inside every ``phase`` and inside the rig's restore executor."""
    watch = During()
    real_phase = phase_module.phase

    def watched(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        watch.sample()
        return real_phase(*args, **kwargs)

    importers = [
        module
        for name, module in list(sys.modules.items())
        if name.startswith("l3io.wp.backup.")
        and getattr(module, "phase", None) is real_phase
    ]
    assert len(importers) >= 2, importers
    for module in importers:
        monkeypatch.setattr(module, "phase", watched)

    def execute(plan: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        watch.sample()
        return report_for(plan)

    rig.execute = execute
    return watch


def _damage(source: Path, copy: Path) -> None:
    body = bytearray(source.read_bytes())
    body[len(body) // 2] ^= 0xFF
    copy.write_bytes(bytes(body))


def _calls(site: Path, tmp_path: Path, rig: Rig) -> dict[str, Callable[[], object]]:
    final = tmp_path / "state.tar.gz"
    damaged = tmp_path / "damaged.tar.gz"
    return {
        "backup": lambda: api.backup(site, scope=BackupScope.ALL, target=final),
        "verify": lambda: api.verify(final),
        "verify-fails": lambda: (_damage(final, damaged), api.verify(damaged)),
        "restore": lambda: api.restore(
            rig.site_root,
            archive=rig.archive_path,
            mode=RestoreMode.ALL,
            overrides=ALL_WAIVED,
            snapshot=SnapshotPolicy.TAKE,
            config_policy=ConfigPolicy.KEEP,
            snapshot_dir=rig.work / "snapshots",
        ),
        "restore-refused": lambda: api.restore(
            rig.site_root / "missing",
            archive=rig.archive_path,
            mode=RestoreMode.ALL,
            overrides=ALL_WAIVED,
            snapshot=SnapshotPolicy.TAKE,
            config_policy=ConfigPolicy.KEEP,
        ),
    }


def test_no_entry_point_changes_logging_or_signal_state(
    site: Path, tmp_path: Path, rig: Rig, during: During
) -> None:
    before = _state()
    outcomes: dict[str, str] = {}
    for name, call in _calls(site, tmp_path, rig).items():
        during.entry = name
        try:
            call()
            outcomes[name] = "returned"
        except Exception as exc:  # every outcome is recorded and asserted below
            outcomes[name] = type(exc).__name__
        assert _state() == before, name
    assert outcomes["backup"] == outcomes["verify"] == outcomes["restore"] == "returned"
    assert outcomes["verify-fails"] == "VerificationFailedError"
    assert outcomes["restore-refused"] != "returned"
    assert during.entries() >= {"backup", "verify", "verify-fails", "restore"}
    assert [entry for entry, state in during.seen if state != before] == []


@pytest.mark.parametrize("entry", ["backup", "verify", "restore"])
def test_the_check_sees_a_handler_installed_during_a_run(
    entry: str,
    site: Path,
    tmp_path: Path,
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
    during: During,
) -> None:
    """The planted case: a driver that swaps ``SIGTERM`` and restores it."""
    driver = {"backup": "drive_backup", "verify": "drive_verify"}.get(
        entry, "drive_restore"
    )
    real = getattr(operations, driver)

    def swapping(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        previous = signal.signal(signal.SIGTERM, lambda *_: None)
        try:
            return real(*args, **kwargs)
        finally:
            signal.signal(signal.SIGTERM, previous)

    calls = _calls(site, tmp_path, rig)
    if entry == "verify":
        calls["backup"]()
    monkeypatch.setattr(operations, driver, swapping)
    before = _state()
    during.entry = entry
    calls[entry]()
    assert _state() == before
    assert entry in {e for e, state in during.seen if state != before}


# --------------------------------------------------------------------------
# AST: api/ installs nothing process-wide
# --------------------------------------------------------------------------

_FORBIDDEN = frozenset(
    {"signal.signal", "signal.setitimer", "structlog.configure", "logging.basicConfig"}
)


def _installers(directory: Path) -> list[str]:
    """Matched on the qualified callee, so an alias or a from-import is seen."""
    found: list[str] = []
    for path in sorted(directory.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        package = module_package(
            path, root=directory, root_package="l3io.wp.backup.api"
        )
        table = ImportTable(tree, package=package)
        found += [
            f"{path.relative_to(directory)}:{line}"
            for line, name in table.calls(tree)
            if name in _FORBIDDEN or name.rsplit(".", 1)[-1] == "configure_logging"
        ]
    return sorted(
        found, key=lambda text: (text.rsplit(":", 1)[0], int(text.rsplit(":", 1)[1]))
    )


def test_the_scope_is_every_api_module() -> None:
    assert {"__init__.py", "operations.py"} <= {p.name for p in API.rglob("*.py")}


def test_api_installs_no_handler_and_configures_no_logging() -> None:
    assert _installers(API) == []


def test_the_scan_fires_on_planted_calls(tmp_path: Path) -> None:
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    (deep / "x.py").write_text(
        "import signal, logging\n"
        "signal.signal(signal.SIGTERM, h)\n"
        "signal.setitimer(signal.ITIMER_REAL, 1)\n"
        "logging.basicConfig()\n"
        "def f():\n    cli_logging.configure_logging()\n"
        "signal.getsignal(signal.SIGTERM)\n",
        encoding="utf-8",
    )
    assert _installers(tmp_path) == [
        "a/b/x.py:2",
        "a/b/x.py:3",
        "a/b/x.py:4",
        "a/b/x.py:6",
    ]


@pytest.mark.parametrize(
    "source",
    [
        "from signal import signal\nsignal(15, h)\n",
        "import signal as s\ns.signal(15, h)\n",
        "from signal import setitimer as t\nt(0, 1)\n",
        "from structlog import configure\nconfigure()\n",
        "import structlog as sl\nsl.configure()\n",
        "from logging import basicConfig as b\nb()\n",
        "from ..cli.logging import configure_logging as c\nc()\n",
    ],
    ids=["from", "module-alias", "from-alias", "structlog", "sl", "basic", "relative"],
)
def test_every_import_spelling_is_resolved(tmp_path: Path, source: str) -> None:
    (tmp_path / "planted.py").write_text(source, encoding="utf-8")
    assert _installers(tmp_path) == ["planted.py:2"]
