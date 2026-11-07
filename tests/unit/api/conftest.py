"""Fixtures for the library entry points' unit tests (E006-S01-001).

The doubles are Epic 2's and Epic 3's, imported, never rewritten: ``site``,
``workdir`` and ``fake_database`` from the CLI driver tests, and the restore
root's :class:`~tests.restore_root_rig.Rig`. No passphrase is in the
environment unless a test plants one, so the AD-26 chain resolves only what a
test gives it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from l3io.wp.backup.cli.passphrase import PASSPHRASE_ENV
from tests.restore_root_rig import Rig
from tests.unit.cli.conftest import (  # noqa: F401 - fixtures, re-registered here
    fake_database,
    site,
    workdir,
)

#: database's passphrase backend; ``binder`` needs no external ``age`` (A17).
BACKEND_ENV = "L3IO_AGE_PASSPHRASE_BACKEND"


@pytest.fixture(autouse=True)
def _no_passphrase(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PASSPHRASE_ENV, raising=False)
    monkeypatch.setenv(BACKEND_ENV, "binder")


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    return Rig(tmp_path, monkeypatch)
