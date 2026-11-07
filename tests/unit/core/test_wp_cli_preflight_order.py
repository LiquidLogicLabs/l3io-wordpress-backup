"""E007-S01-004 (FR-13, AD-13) — no WP-CLI, nothing touched.

Driven through ``execute_restore`` over :mod:`tests.restore_harness`, with the
plan built the way the restore root builds it — ``build_restore_plan(...,
extra_tools=ext.required_tools())`` over Epic 7's real extension — and the
preflight composed as the root will compose it: ``require_tools`` over E005's
real ``BinaryPreflight``. ``wp`` is controlled by ``PATH``: it holds only a
temp directory, empty or with a fake ``wp`` script, so the result never depends
on what this host has installed.

The ``database-dump`` requirement is not a binary (ADR-0015), so a small
router answers it as ready and hands every other requirement to
``BinaryPreflight``, in plan order.

Asserted for a refusal, in a real Run and under ``dry_run_adapters`` alike:
``PreflightFailedError`` (exit 7) with the built rendering, an empty command
log, an Archive never verified (``VERIFY``'s scan never called), no
``begin_run`` and no Safety Snapshot. Without a URL relocation ``wp`` is never
looked up, and a fake ``wp`` that writes a marker when run never runs.
"""

from __future__ import annotations

import dataclasses
import functools
import os
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("l3io.wp.database")

from l3io.wp.backup.adapters import preflight as preflight_module
from l3io.wp.backup.adapters import restore_runner
from l3io.wp.backup.adapters.noop import ActionLog
from l3io.wp.backup.adapters.preflight import BinaryPreflight
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.adapters.site_files import LocalSiteFiles
from l3io.wp.backup.adapters.wpcli import WpCliRestoreExtension, restore_extension
from l3io.wp.backup.cli.adapter_sets import dry_run_adapters
from l3io.wp.backup.cli.preflight import require_tools
from l3io.wp.backup.cli.restore import RelocationFlags
from l3io.wp.backup.core.backup import DUMP_REQUIREMENT
from l3io.wp.backup.core.relocation import WP_CLI_MINIMUM, WP_CLI_REQUIREMENT
from l3io.wp.backup.core.restore import NO_RELOCATION, build_restore_plan
from l3io.wp.backup.domain.errors import ExitCode, PreflightFailedError
from l3io.wp.backup.domain.preflight import UNKNOWN_VERSION, ToolRequirement, ToolStatus
from l3io.wp.backup.domain.restore import RestoreMode, RestorePhase, SnapshotPolicy
from l3io.wp.backup.ports.adapter_set import AdapterSet
from l3io.wp.backup.ports.restore import RestorePorts, restore_ports
from tests.restore_harness import Harness, write_v1

MINIMUM = ".".join(map(str, WP_CLI_MINIMUM))
URLS = dataclasses.replace(
    RelocationFlags.none(),
    new_site_url="https://staging.e007.example",
    new_home_url="https://www.staging.e007.example",
)
SELECTIONS = ("dry_run", "real")


class Routed:
    """``database-dump`` ready; every binary to ``BinaryPreflight``; plan order."""

    def __init__(self, binaries: BinaryPreflight, looked_up: list[str]) -> None:
        self._binaries, self.looked_up = binaries, looked_up

    def check(self, requirements: Sequence[ToolRequirement]) -> Sequence[ToolStatus]:
        rest = [r for r in requirements if r.name != DUMP_REQUIREMENT.name]
        self.looked_up.extend(r.name for r in rest)
        found = {s.name: s for s in self._binaries.check(rest)} if rest else {}
        return [
            found.get(r.name)
            or ToolStatus(name=r.name, present=True, version=None, path=None)
            for r in requirements
        ]


@dataclasses.dataclass
class Rig:
    h: Harness
    bin: Path
    marker: Path
    looked_up: list[str]
    verified: list[Path]
    #: What the Dry Run's no-op command halves (journal included) were asked.
    skipped: ActionLog = dataclasses.field(default_factory=ActionLog)

    def fake_wp(self, body: str) -> Path:
        """A ``wp`` on ``PATH`` that records it ran, then runs ``body``."""
        script = self.bin / "wp"
        script.write_text(
            f"#!/bin/sh\necho ran >> '{self.marker}'\n{body}\n", encoding="utf-8"
        )
        script.chmod(0o755)
        return script

    def extension(self, flags: RelocationFlags) -> WpCliRestoreExtension:
        """Epic 7's factory, as the root calls it: ``wp`` resolved on ``PATH`` once."""
        return restore_extension(flags, secrets=SecretRegistry())

    def ports(self, adapters: AdapterSet) -> RestorePorts:
        checker = Routed(BinaryPreflight(cwd=self.h.workspace), self.looked_up)
        return restore_ports(
            adapters,
            preflight=functools.partial(require_tools, checker),
            free_bytes=lambda _path: 10**12,
            cutover_credentials=None,
        )

    def run(
        self,
        flags: RelocationFlags,
        *,
        selection: str = "real",
        mode: RestoreMode = RestoreMode.ALL,
    ) -> object:
        ext = self.extension(flags)
        relocation = NO_RELOCATION
        if flags != RelocationFlags.none():
            relocation = ext.resolve(
                site_root=self.h.site_root,
                site=self.h.site,
                view=self.h.view,
                files=LocalSiteFiles(),
                mode=mode,
            )
        self.h.mode, self.h.relocation = mode, relocation
        plan = build_restore_plan(
            self.h.site,
            mode,
            self.h.overrides,
            self.h.view,
            self.h.target(),
            archive=self.h.archive,
            relocation=relocation,
            snapshot=self.h.snapshot,
            snapshot_dir=self.h.work / "snapshots",
            config_dir=self.h.site_root,
            config_policy=self.h.config_policy,
            extra_tools=ext.required_tools(),
        )
        self.h.ports = self.ports  # type: ignore[method-assign,assignment]
        adapters = self.h.adapters()
        if selection == "dry_run":
            adapters = dry_run_adapters(adapters, log=self.skipped)
        journal = adapters.journal_command
        return self.h.run(plan=plan, adapters=adapters, journal=journal)


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    binaries = tmp_path / "bin"
    binaries.mkdir()
    monkeypatch.setenv("PATH", str(binaries))
    work = tmp_path / "work"
    work.mkdir()
    h = Harness(work=work, archive_path=write_v1(work), snapshot=SnapshotPolicy.TAKE)
    verified: list[Path] = []
    real_verify = restore_runner.verify_archive_manifest

    def counting(path: Path, **kwargs: Any) -> Any:  # noqa: ANN401
        verified.append(path)
        return real_verify(path, **kwargs)

    monkeypatch.setattr(restore_runner, "verify_archive_manifest", counting)
    return Rig(
        h=h,
        bin=binaries,
        marker=tmp_path / "wp-ran",
        looked_up=[],
        verified=verified,
    )


def _refused(rig: Rig, selection: str) -> PreflightFailedError:
    config = rig.h.config_path.read_bytes()
    with pytest.raises(PreflightFailedError) as caught:
        rig.run(URLS, selection=selection)
    assert caught.value.exit_code is ExitCode.NOT_READY
    assert rig.h.log.calls == [], "a command adapter was called"
    assert list(rig.skipped.calls) == [], "a Dry Run command (or begin_run) ran"
    assert rig.verified == [], "the Archive was verified"
    assert rig.h.journal.events == [], "the journal was begun"
    assert rig.h.snapshots == [], "a Safety Snapshot was taken"
    assert rig.h.config_path.read_bytes() == config
    assert rig.looked_up == ["wp"]
    return caught.value


def _rendering(found: str) -> str:
    return (
        f"wp is needed for {WP_CLI_REQUIREMENT.needed_for}; found: {found}\n"
        f"install: {WP_CLI_REQUIREMENT.install_hint}"
    )


# --------------------------------------------------------------------------
# The plan carries WP-CLI only for a URL relocation
# --------------------------------------------------------------------------


def test_the_scope_reaches_preflight_through_the_extension(rig: Rig) -> None:
    """The one route: ``ext.required_tools()``; ``RestoreRelocation``'s stays empty."""
    ext = rig.extension(URLS)
    relocation = ext.resolve(
        site_root=rig.h.site_root,
        site=rig.h.site,
        view=rig.h.view,
        files=LocalSiteFiles(),
        mode=RestoreMode.ALL,
    )
    assert relocation.required_tools == ()
    assert ext.required_tools() == (WP_CLI_REQUIREMENT,)
    assert rig.extension(RelocationFlags.none()).required_tools() == ()


# --------------------------------------------------------------------------
# Refused before the first effect, real and Dry Run alike
# --------------------------------------------------------------------------


@pytest.mark.parametrize("selection", sorted(SELECTIONS))
def test_no_wp_refuses_before_anything_is_touched(rig: Rig, selection: str) -> None:
    error = _refused(rig, selection)
    assert str(error) == _rendering("absent")
    assert "wp-cli.org" in str(error)
    assert "SEARCH_REPLACE" in str(error)


@pytest.mark.parametrize(
    ("body", "found"),
    [
        ("echo 'WP-CLI 2.0.0'", "2.0.0"),
        ("exit 127", "unusable (exit 127)"),
        ("echo 'WP-CLI nightly'", UNKNOWN_VERSION),
    ],
    ids=["too-old", "unrunnable", "unparsable"],
)
@pytest.mark.parametrize("selection", sorted(SELECTIONS))
def test_an_unusable_wp_refuses_before_anything_is_touched(
    rig: Rig, selection: str, body: str, found: str
) -> None:
    rig.fake_wp(body)
    error = _refused(rig, selection)
    assert str(error).startswith(_rendering(found).split("\n", 1)[0]), str(error)
    assert error.found.startswith(found)


def test_the_dry_run_refuses_with_the_identical_message(rig: Rig) -> None:
    real = _refused(rig, "real")
    rig.looked_up.clear()
    assert str(_refused(rig, "dry_run")) == str(real)


def test_a_wp_at_the_minimum_passes_and_the_restore_proceeds(rig: Rig) -> None:
    rig.fake_wp(f"echo 'WP-CLI {MINIMUM}'")
    rig.run(URLS)
    assert rig.looked_up == ["wp"]
    assert rig.marker.read_text(encoding="utf-8") == "ran\n"
    assert rig.verified == [rig.h.archive_path]
    assert "search_replace.relocate_content" in rig.h.log.methods()


# --------------------------------------------------------------------------
# Without a URL relocation, wp is irrelevant
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("flags", "mode"),
    [
        (RelocationFlags.none(), RestoreMode.ALL),
        (
            dataclasses.replace(RelocationFlags.none(), new_db_host="db.e007:3307"),
            RestoreMode.DATABASE,
        ),
    ],
    ids=["no-relocation", "new-db-host-only"],
)
def test_without_a_url_wp_is_never_looked_up_or_run(
    rig: Rig, flags: RelocationFlags, mode: RestoreMode
) -> None:
    rig.fake_wp("exit 127")
    rig.run(flags, mode=mode)
    assert "wp" not in rig.looked_up
    assert not rig.marker.exists(), "wp was run"
    assert rig.h.journal.events, "the restore did not proceed"
    assert "search_replace.relocate_content" not in rig.h.log.methods()
    assert RestorePhase.SEARCH_REPLACE not in {c[0] for c in rig.h.log.calls}


# --------------------------------------------------------------------------
# One wp path (the residual is the docstring's)
# --------------------------------------------------------------------------


def test_the_checked_wp_is_the_wp_the_extension_runs(rig: Rig) -> None:
    script = rig.fake_wp(f"echo 'WP-CLI {MINIMUM}'")
    ext = rig.extension(URLS)
    (status,) = BinaryPreflight(cwd=rig.h.workspace).check([WP_CLI_REQUIREMENT])
    assert status.path is not None
    assert ext._wp == status.path == str(script)  # noqa: SLF001
    assert shutil.which("wp") == str(script)
    assert os.environ["PATH"] == str(rig.bin)


@pytest.mark.parametrize("entry", ["", ".", "rel"], ids=["empty", "dot", "relative"])
def test_a_relative_path_entry_cannot_split_checked_from_run(
    rig: Rig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
) -> None:
    """Attacks the lookup's scope: a ``wp`` only a relative entry finds, first."""
    script = rig.fake_wp(f"echo 'WP-CLI {MINIMUM}'")
    cwd = tmp_path / "cwd"
    shadow_dir = cwd / (entry or ".")
    shadow_dir.mkdir(parents=True, exist_ok=True)
    shadow = shadow_dir / "wp"
    shadow.write_text("#!/bin/sh\nexit 127\n", encoding="utf-8")
    shadow.chmod(0o755)
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("PATH", os.pathsep.join([entry, str(rig.bin)]))
    assert shutil.which("wp") != str(script), "the shadow is not found first"
    ext = rig.extension(URLS)
    (status,) = BinaryPreflight(cwd=rig.h.workspace).check([WP_CLI_REQUIREMENT])
    assert ext._wp == status.path == str(script)  # noqa: SLF001


def test_relocation_s_requirement_is_registered_for_the_docs() -> None:
    """FR-27: the README's table is generated from these sources."""
    produced = [
        r
        for source in preflight_module.DOCUMENTED_REQUIREMENT_SOURCES
        for r in source()
    ]
    assert WP_CLI_REQUIREMENT in produced
