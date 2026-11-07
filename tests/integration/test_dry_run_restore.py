"""AD-4 — a Dry Run restore changes nothing and refuses where the real Run does.

``execute_restore`` is driven under ``dry_run_adapters(...)`` over the real
site-files adapter and config writer for the file Modes. The Site Root tree
and the Storage Target listing are unchanged; a refusal is the real Run's, with
the same ``ExitCode``.

Not covered here (BL-E003-030): the snapshot and journal directories. In this
harness ``take_snapshot`` is a fake that writes nothing and the journal is
in-memory, so asserting those directories unchanged could not fail. They are
covered with the real slots by the CLI-level ``--whatIf`` per Mode test in
``tests/integration/test_restore_cli.py`` (E003-S02-006).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from l3io.wp.backup.adapters.config_writer import WpConfigWriter
from l3io.wp.backup.adapters.noop import ActionLog
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.cli.adapter_sets import dry_run_adapters
from l3io.wp.backup.core.restore import InsufficientSpaceError
from l3io.wp.backup.domain.errors import BackupError, VerificationFailedError
from l3io.wp.backup.domain.restore import RestoreMode
from tests.restore_harness import Harness, write_v1

pytestmark = pytest.mark.integration

FILE_MODES = [RestoreMode.FILES, RestoreMode.FILES_CLEAN]


def _state(*roots: Path) -> dict[str, tuple[bytes | None, int]]:
    found: dict[str, tuple[bytes | None, int]] = {}
    for root in roots:
        for path in sorted(root.rglob("*")):
            body = path.read_bytes() if path.is_file() else None
            found[str(path)] = (body, path.stat().st_mtime_ns)
    return found


def _harness(tmp_path: Path, mode: RestoreMode) -> Harness:
    h = Harness(
        work=tmp_path, archive_path=write_v1(tmp_path), mode=mode, real_files=True
    )
    h.config = WpConfigWriter(SecretRegistry())  # type: ignore[assignment]
    return h


def _roots(h: Harness) -> tuple[Path, ...]:
    return (h.site_root,)


@pytest.mark.parametrize("mode", FILE_MODES)
def test_a_dry_run_changes_nothing(tmp_path: Path, mode: RestoreMode) -> None:
    h = _harness(tmp_path, mode)
    listing = sorted(p.name for p in h.archive_path.parent.iterdir())
    before = _state(*_roots(h))
    skipped = ActionLog()
    rehearsal = dry_run_adapters(h.adapters(), log=skipped)
    plan = h.plan()
    report = h.run(plan=plan, adapters=rehearsal, journal=rehearsal.journal_command)
    assert _state(*_roots(h)) == before
    assert sorted(p.name for p in h.archive_path.parent.iterdir()) == listing
    assert report.phases == plan.phases
    assert [a.ordinal for a in plan.actions] == list(range(1, len(plan.actions) + 1))
    assert [c.method for c in skipped.calls if c.adapter == "NoopSiteFiles"] == (
        ["clean", "extract"] if mode is RestoreMode.FILES_CLEAN else ["extract"]
    )


def _refusal(h: Harness, *, rehearse: bool) -> BackupError:
    adapters = h.adapters()
    if rehearse:
        adapters = dry_run_adapters(adapters)
    with pytest.raises(BackupError) as caught:
        h.run(adapters=adapters, journal=adapters.journal_command)
    return caught.value


@pytest.mark.parametrize("mode", FILE_MODES)
def test_a_dry_run_refuses_where_the_real_run_does(
    tmp_path: Path, mode: RestoreMode
) -> None:
    h = _harness(tmp_path, mode)
    h.free = 10
    rehearsed, real = _refusal(h, rehearse=True), _refusal(h, rehearse=False)
    assert type(rehearsed) is type(real) is InsufficientSpaceError
    assert rehearsed.exit_code is real.exit_code

    h.free = 10**12
    data = h.archive_path.read_bytes()
    h.archive_path.write_bytes(data[: len(data) // 2])
    rehearsed, real = _refusal(h, rehearse=True), _refusal(h, rehearse=False)
    assert type(rehearsed) is type(real) is VerificationFailedError
    assert rehearsed.exit_code is real.exit_code
