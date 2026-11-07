"""E007-S01-004 (FR-13, AD-13) — no usable WP-CLI, nothing touched (CI database).

Each case runs ``execute_restore`` over the real adapters
(``tests/integration/live_restore``), with the plan built through Epic 7's
real extension — ``ext.resolve`` and ``extra_tools=ext.required_tools()``, as
the restore root does — and the preflight composed in the test as
``functools.partial(require_tools, BinaryPreflight(cwd=<workspace>))`` (C5).
``wpbackup2 restore`` itself binds ``DumpReadinessPreflight`` until
E008-S01-001's root preflight, so the CLI cases are E008-S01-001's (X1).

``wp`` is controlled by ``PATH``, never by uninstalling it: ``PATH`` is a temp
directory holding links to the database client tools found on the original
``PATH`` (whatever of :data:`CLIENT_TOOLS` exists) and, per case, a fake
``wp`` script that appends to a marker file when run.

1. no ``wp``: ``PreflightFailedError`` (exit 7) naming ``wp``,
   ``SEARCH_REPLACE`` and ``wp-cli.org``; the Site Root tree, the
   ``wp-config.php`` bytes, a ``CHECKSUM TABLE`` of the live database and the
   set of databases are unchanged; no journal ``current.json`` exists;
2. a ``wp`` printing ``WP-CLI 2.0.0``: exit 7, ``found: 2.0.0``, and the
   preflight record carries the minimum;
3. a ``wp`` exiting 127 (no PHP): exit 7, ``found: unusable (exit 127 ...)``;
4. the same restore without URL flags: it completes and ``wp`` never ran;
5. ``--new-db-host`` alone, ``--mode database``: it completes and the
   (unrunnable) ``wp`` never ran;
6. a Dry Run of case 1: exit 7 with the identical message.

ED-1 (E008-S01-001): with the roots' ``RootPreflight`` bound, a relocating
Dry Run through ``wpbackup2 restore --whatIf`` and through ``api.restore``
with a ``wp`` printing ``WP-CLI 2.12.0`` on ``PATH`` passes ``PREFLIGHT``:
the root probes that very ``wp`` (``cli version --skip-plugins --skip-themes
--skip-packages --no-color``) and plans ``SEARCH_REPLACE``.

Needs the CI ``db`` service (``DB_HOST``/``DB_PASSWORD``); skips without it.
"""

from __future__ import annotations

import dataclasses
import functools
import hashlib
import os
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from structlog.testing import capture_logs

pytest.importorskip("l3io.wp.database")

from l3io.wp.backup import api
from l3io.wp.backup.adapters.database import site_descriptor_from_wp_config
from l3io.wp.backup.adapters.journal import CURRENT
from l3io.wp.backup.adapters.preflight import BinaryPreflight
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.adapters.site_files import LocalSiteFiles
from l3io.wp.backup.adapters.tar import verify_archive_view
from l3io.wp.backup.adapters.wpcli import restore_extension
from l3io.wp.backup.cli.adapter_sets import dry_run_adapters
from l3io.wp.backup.cli.preflight import require_tools
from l3io.wp.backup.cli.restore import RelocationFlags
from l3io.wp.backup.core.relocation import WP_CLI_MINIMUM, WP_CLI_REQUIREMENT
from l3io.wp.backup.core.restore import RestoreRelocation
from l3io.wp.backup.domain.credentials import ExplicitCredentialSpec
from l3io.wp.backup.domain.errors import ExitCode, PreflightFailedError
from l3io.wp.backup.domain.restore import (
    ConfigPolicy,
    RestoreMode,
    RestorePhase,
    SnapshotPolicy,
)
from l3io.wp.backup.ports.adapter_set import AdapterSet
from tests.integration.live_cli import (
    CliSite,
    admin_account,
    authorizing_flags,
    cli_site,
    run_cli,
)
from tests.integration.live_restore import (
    LiveSite,
    build_archive,
    host,
    journal_for,
    live_site,
    port,
    restore,
    spec_for,
)
from tests.restore_root_rig import ALL_WAIVED

__all__ = ["admin_account", "cli_site", "live_site"]

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        "DB_HOST" not in os.environ, reason="needs the CI database (DB_HOST unset)"
    ),
]

#: Linked into the controlled ``PATH`` when the original ``PATH`` has them.
CLIENT_TOOLS = ("mysql", "mysqldump", "mariadb", "mariadb-dump", "php")
URLS = dataclasses.replace(
    RelocationFlags.none(),
    new_site_url="https://staging.e007.example",
    new_home_url="https://www.staging.e007.example",
)
MINIMUM = ".".join(map(str, WP_CLI_MINIMUM))


@dataclasses.dataclass
class Rig:
    site: LiveSite
    bin: Path
    marker: Path
    archive: Path

    def fake_wp(self, body: str) -> None:
        script = self.bin / "wp"
        script.write_text(
            f"#!/bin/sh\necho ran >> '{self.marker}'\n{body}\n", encoding="utf-8"
        )
        script.chmod(0o755)

    def run(
        self,
        flags: RelocationFlags,
        *,
        mode: RestoreMode = RestoreMode.ALL,
        select: Callable[[AdapterSet], AdapterSet] | None = None,
    ) -> object:
        site = self.site
        ext = restore_extension(flags, secrets=SecretRegistry())
        relocation = RestoreRelocation.none()
        if flags != RelocationFlags.none():
            descriptor, _ = site_descriptor_from_wp_config(
                site.site_root, config_file=site.config
            )
            view, _ = verify_archive_view(self.archive)
            assert view is not None
            relocation = ext.resolve(
                site_root=site.site_root,
                site=descriptor,
                view=view,
                files=LocalSiteFiles(),
                mode=mode,
            )
        spec = spec_for(site, self.archive, mode, relocation=relocation)
        workspace = site.work / f"preflight-{spec.run_id}"
        workspace.mkdir(mode=0o700)
        return restore(
            spec,
            extra_tools=ext.required_tools(),
            preflight=functools.partial(require_tools, BinaryPreflight(cwd=workspace)),
            select=select,
        )


@pytest.fixture
def rig(live_site: LiveSite, monkeypatch: pytest.MonkeyPatch) -> Rig:
    binaries = live_site.work / "controlled-bin"
    binaries.mkdir()
    for name in CLIENT_TOOLS:
        found = shutil.which(name)
        if found is not None:
            (binaries / name).symlink_to(found)
    monkeypatch.setenv("PATH", str(binaries))
    assert shutil.which("wp") is None
    return Rig(
        site=live_site,
        bin=binaries,
        marker=live_site.work / "wp-ran",
        archive=build_archive(live_site.work, config=None),
    )


def _tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        digest.update(str(path.relative_to(root)).encode())
        if path.is_file() and not path.is_symlink():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _untouched_after(
    rig: Rig, *, select: Callable[[AdapterSet], AdapterSet] | None = None
) -> PreflightFailedError:
    site = rig.site
    tree, config = _tree(site.site_root), site.config.read_bytes()
    checksums, databases = site.checksums(), site.databases()
    with pytest.raises(PreflightFailedError) as caught:
        rig.run(URLS, select=select)
    assert caught.value.exit_code is ExitCode.NOT_READY
    assert caught.value.tool == "wp"
    assert _tree(site.site_root) == tree
    assert site.config.read_bytes() == config
    assert site.checksums() == checksums
    assert site.databases() == databases
    assert not (journal_for(site.site_root).directory / CURRENT).exists()
    return caught.value


def test_1_no_wp_refuses_with_nothing_changed(rig: Rig) -> None:
    error = _untouched_after(rig)
    text = str(error)
    assert text.startswith("wp is needed for SEARCH_REPLACE"), text
    assert "found: absent" in text
    assert "wp-cli.org" in text


def test_2_an_old_wp_refuses_naming_found_and_minimum(rig: Rig) -> None:
    rig.fake_wp("echo 'WP-CLI 2.0.0'")
    with capture_logs() as logs:
        error = _untouched_after(rig)
    assert error.found == "2.0.0"
    assert "found: 2.0.0" in str(error)
    (checked,) = [r for r in logs if r["event"] == "preflight_checked"]
    assert checked["minimum"] == MINIMUM
    assert rig.marker.exists(), "the probe did not execute wp"


def test_3_an_unrunnable_wp_refuses_as_unusable(rig: Rig) -> None:
    rig.fake_wp("exit 127")
    error = _untouched_after(rig)
    assert error.found.startswith("unusable (exit 127"), error.found


def test_4_without_url_flags_wp_is_never_run(rig: Rig) -> None:
    rig.fake_wp("exit 127")
    rig.run(RelocationFlags.none())
    assert not rig.marker.exists(), "wp was run"


def test_5_a_db_host_relocation_needs_no_wp(rig: Rig) -> None:
    flags = dataclasses.replace(
        RelocationFlags.none(), new_db_host=f"{host()}:{port()}"
    )
    rig.fake_wp("exit 127")
    rig.run(flags, mode=RestoreMode.DATABASE)
    assert not rig.marker.exists(), "wp was run"


def test_6_a_dry_run_refuses_identically(rig: Rig) -> None:
    real = _untouched_after(rig)
    dry = _untouched_after(rig, select=dry_run_adapters)
    assert str(dry) == str(real)
    assert WP_CLI_REQUIREMENT.install_hint is not None
    assert WP_CLI_REQUIREMENT.install_hint in str(dry)


# --------------------------------------------------------------------------
# ED-1 (E008-S01-001): a usable wp passes the roots' own PREFLIGHT
# --------------------------------------------------------------------------

PROBE = "cli version --skip-plugins --skip-themes --skip-packages --no-color"


@pytest.fixture
def wp_2_12(cli_site: CliSite, monkeypatch: pytest.MonkeyPatch) -> Path:
    """``PATH``: the client tools and a ``wp`` 2.12.0 recording its arguments."""
    binaries = cli_site.site.work / "ed1-bin"
    binaries.mkdir()
    for name in CLIENT_TOOLS:
        found = shutil.which(name)
        if found is not None:
            (binaries / name).symlink_to(found)
    marker = cli_site.site.work / "wp-argv"
    script = binaries / "wp"
    script.write_text(
        f"#!/bin/sh\necho \"$@\" >> '{marker}'\necho 'WP-CLI 2.12.0'\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(binaries))
    return marker


def _url_flags() -> list[str]:
    return [
        "--new-site-url",
        str(URLS.new_site_url),
        "--new-site-home-url",
        str(URLS.new_home_url),
    ]


def test_ed1_a_relocating_cli_dry_run_passes_preflight(
    cli_site: CliSite, wp_2_12: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    site = cli_site
    before = (_tree(site.root), site.site.config.read_bytes(), site.site.checksums())
    argv = site.argv(site.archive(), *authorizing_flags(RestoreMode.ALL), *_url_flags())
    # A passing check's ``preflight_checked`` is DEBUG (adapters/preflight.py's
    # ``_log_checked``); the default INFO level shows only ``preflight_passed``.
    ran = run_cli(["--whatIf", "--log-level", "DEBUG", *argv], capsys)
    assert ran.code == int(ExitCode.OK), ran.err[-3000:]
    checked = [r for r in ran.events("preflight_checked") if r.get("tool") == "wp"]
    assert [r["outcome"] for r in checked] == ["ok"]
    assert [r["level"] for r in checked] == ["debug"]
    assert any("wp" in r["tools"] for r in ran.events("preflight_passed"))
    assert PROBE in wp_2_12.read_text(encoding="utf-8")
    assert f"wp >= {MINIMUM}" in ran.out
    assert (
        _tree(site.root),
        site.site.config.read_bytes(),
        site.site.checksums(),
    ) == before


def test_ed1_a_relocating_api_dry_run_passes_preflight(
    cli_site: CliSite, wp_2_12: Path
) -> None:
    site = cli_site
    with capture_logs():
        outcome = api.restore(
            site.root,
            archive=site.archive(),
            mode=RestoreMode.ALL,
            overrides=ALL_WAIVED,
            snapshot=SnapshotPolicy.TAKE,
            config_policy=ConfigPolicy.KEEP,
            snapshot_dir=site.snapshots,
            admin=ExplicitCredentialSpec(site.admin_user, site.admin_password),
            relocation=URLS,
            dry_run=True,
        )
    assert outcome.dry_run
    assert RestorePhase.PREFLIGHT in outcome.phases
    assert RestorePhase.SEARCH_REPLACE in {a.phase for a in outcome.actions}
    assert PROBE in wp_2_12.read_text(encoding="utf-8")
