"""Owner decision 41 (BL-E003-021, ADV-14): a Dry Run lists the links it would skip.

Driven through ``main`` over :class:`tests.restore_root_rig.Rig` with a real v1
Archive holding an absolute, an escaping and an inside symlink: the Dry Run
lists, after its actions, the two the real Run would skip with each one's
reason (text) or in ``skipped_links`` with ``reason`` (``--log-format json``);
the inside link is not listed. A v0 Archive records no link targets, so its
Dry Run says they are reported at extraction (``skipped_links`` is ``null``).
A Mode that extracts no Site files lists nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("l3io.wp.database")

from l3io.wp.backup.cli import main
from l3io.wp.backup.cli.restore_report import LINKS_AT_EXTRACTION
from l3io.wp.backup.domain.exit_codes import ExitCode
from tests.restore_harness import write_v0, write_v1
from tests.restore_root_rig import Rig, report_for

#: Each link the Archive holds, and the reason extraction skips it (or None).
LINKS = {
    "abs": ("/etc/passwd", "absolute"),
    "up": ("../outside", "outside_destination"),
    "inside": ("index.php", None),
}


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    rig = Rig(tmp_path, monkeypatch)
    rig.execute = lambda plan, *a, **k: report_for(plan, performed=False)
    return rig


def _linked(rig: Rig, capsys: pytest.CaptureFixture[str]) -> None:
    work = rig.work / "linked"
    work.mkdir()
    links = {name: target for name, (target, _) in LINKS.items()}
    rig.archive_path = write_v1(work, links=links)
    # The writer's own WARNINGs (symlink_outside_site_root) are not the Run's.
    capsys.readouterr()


def _v0(rig: Rig) -> None:
    work = rig.work / "v0"
    work.mkdir()
    rig.archive_path = write_v0(work)


def _argv(rig: Rig, *extra: str, fmt: str = "console") -> list[str]:
    return [
        "--log-format",
        fmt,
        "--whatIf",
        "restore",
        "--wp-dir",
        str(rig.site_root),
        "--archive",
        str(rig.archive_path),
        "--snapshot-dir",
        str(rig.work / "snapshots"),
        "--force",
        *extra,
    ]


def _skipped() -> dict[str, tuple[str, str]]:
    return {n: (t, r) for n, (t, r) in LINKS.items() if r is not None}


def test_a_dry_run_lists_the_links_it_would_skip_after_its_actions(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    _linked(rig, capsys)
    assert main(_argv(rig, "--mode", "files")) == int(ExitCode.OK)
    out = capsys.readouterr().out.splitlines()
    last_action = max(out.index(a.text) for a in rig.plans[-1].actions)
    listed = [line for line in out if line.startswith("would skip symlink")]
    assert all(out.index(line) > last_action for line in listed)
    assert set(listed) == {
        "would skip symlink 'abs' -> '/etc/passwd': "
        "its target is absolute (not supported)",
        "would skip symlink 'up' -> '../outside': "
        "its target resolves outside the Site Root (not supported)",
    }
    assert not any("'inside'" in line for line in out)


def test_a_dry_runs_json_names_each_skipped_link_and_its_reason(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    _linked(rig, capsys)
    assert main(_argv(rig, "--mode", "files", fmt="json")) == int(ExitCode.OK)
    (line,) = capsys.readouterr().out.splitlines()
    summary = json.loads(line)["restore"]
    assert summary["status"] == "planned"
    assert {
        link["path"]: (link["link_target"], link["reason"])
        for link in summary["skipped_links"]
    } == _skipped()


def test_a_v0_dry_run_says_its_links_are_reported_at_extraction(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    _v0(rig)
    assert main(_argv(rig, "--mode", "files")) == int(ExitCode.OK)
    out = capsys.readouterr().out.splitlines()
    last_action = max(out.index(a.text) for a in rig.plans[-1].actions)
    assert out.index(LINKS_AT_EXTRACTION) > last_action
    assert main(_argv(rig, "--mode", "files", fmt="json")) == int(ExitCode.OK)
    (line,) = capsys.readouterr().out.splitlines()
    assert json.loads(line)["restore"]["skipped_links"] is None


def test_a_mode_that_extracts_no_files_lists_no_links(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    _linked(rig, capsys)
    assert main(_argv(rig, "--mode", "db")) == int(ExitCode.OK)
    out = capsys.readouterr().out
    assert "would skip symlink" not in out
    assert LINKS_AT_EXTRACTION not in out


def test_a_real_run_reports_only_what_extraction_skipped(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    """The real Run's list is extraction's own (here none), never the plan's."""
    _linked(rig, capsys)
    rig.execute = lambda plan, *a, **k: report_for(plan)
    real = [a for a in _argv(rig, "--mode", "files") if a != "--whatIf"]
    assert main(real) == int(ExitCode.OK)
    captured = capsys.readouterr()
    assert "would skip symlink" not in captured.out
    assert "WARNING: symlink" not in captured.err
