"""E003-S02-006 — ``wpbackup2 restore`` end to end, every Mode, over the CI database.

The every-mode integration (Gate AG-3). The Modes are the ``FLAG_MAP`` keys
that express one, typed as the operator types them (``render_flags``), so a
row added to the table is a case here without editing this file:

- each row, with the waivers it needs, against a populated live Site: one
  ``run_id``, ``restore_mode_resolved`` once, ``phase_completed`` in AD-17
  order, ``target_checked``, the Mode in the Safety Snapshot's Manifest
  ``origin``, the unencrypted-snapshot warning, and the parts it restores
  changed and nothing else;
- ``--whatIf`` per Mode changes nothing: Site Root, every table's
  ``CHECKSUM TABLE``, the Archive, the snapshot and journal directories;
- each refusing combination exits ``REFUSED`` naming the authorizing flags
  and logs ``restore_refused``;
- an empty directory as target takes its descriptor from the Archive, and is
  refused without ``--archive-database``;
- a files-only v0 Archive with ``--mode db`` is refused naming the Dump;
- ``--overwrite-wp-config`` with a v0 Archive carrying
  ``wp-root/wp-config.php`` replaces the target's config, keeping its DB_*.

Needs the CI ``db`` service (``DB_HOST``/``DB_PASSWORD``); skips without it.
Every database and account is uniquely named and dropped by CI's connection.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

pytest.importorskip("l3io.wp.database")
pytest.importorskip("pymysql")

from l3io.wp.backup.cli.restore import render_flags
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.domain.flag_map import (
    FLAG_MAP,
    FlagCombination,
    FlagRejection,
    authorizing_combination,
)
from l3io.wp.backup.domain.restore import (
    RESTORE_PHASE_ORDER,
    OverrideSet,
    RestoreMode,
    RestorePart,
    clears_site_root,
    removes_database,
    restores_database,
    restores_files,
)
from tests.conftest import CredentialSentinel
from tests.integration.live_cli import (
    CliSite,
    admin_account,  # noqa: F401 - a fixture
    authorizing_flags,
    cli_site,  # noqa: F401 - a fixture
    manifest_origin,
    run_cli,
    state_dirs,
    tree,
    v0_archive,
)
from tests.integration.live_restore import INDEX, config_text, host, read_keys

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        "DB_HOST" not in os.environ, reason="needs the CI database (DB_HOST unset)"
    ),
]

ENTRIES: list[tuple[FlagCombination, tuple[RestoreMode, OverrideSet]]] = [
    (k, e) for k, e in FLAG_MAP.items() if not isinstance(e, FlagRejection)
]


def _parts(mode: RestoreMode) -> list[RestorePart]:
    parts: list[RestorePart] = []
    if restores_files(mode):
        parts.append("files")
    if restores_database(mode):
        parts.append("database")
    return parts


def _waives_all(mode: RestoreMode, overrides: OverrideSet) -> bool:
    return all(overrides.waives(part) for part in _parts(mode))


#: One authorizing combination per Mode: the table's own (scope from FLAG_MAP).
AUTHORIZED = {
    entry[0]: key for key, entry in ENTRIES if _waives_all(entry[0], entry[1])
}
#: Every combination that leaves a part of a populated target unwaived.
REFUSING = [
    (key, entry[0]) for key, entry in ENTRIES if not _waives_all(entry[0], entry[1])
]


def _flags(key: FlagCombination) -> list[str]:
    return render_flags(key).split()


def _live_database(site: CliSite) -> str:
    return str(read_keys(site.site.config)["DB_NAME"])


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_every_mode_has_an_authorizing_row() -> None:
    assert set(AUTHORIZED) == set(RestoreMode)


@pytest.mark.parametrize("mode", list(RestoreMode))
def test_each_mode_end_to_end(
    cli_site: CliSite,  # noqa: F811
    capsys: pytest.CaptureFixture[str],
    credential_sentinel: CredentialSentinel,
    mode: RestoreMode,
) -> None:
    site = cli_site
    archive = site.archive()
    before = site.site.checksums()
    original = site.site.database
    ran = run_cli(site.argv(archive, *_flags(AUTHORIZED[mode])), capsys)
    assert ran.code == int(ExitCode.OK), ran.err[-3000:]
    run_ids = {r["l3io_run_id"] for r in ran.records if "l3io_run_id" in r}
    assert len(run_ids) == 1
    assert len(ran.events("restore_mode_resolved")) == 1
    assert ran.events("restore_mode_resolved")[0]["restore_mode"] == mode.value
    assert ran.events("target_checked")
    assert ran.events("planned_action")
    order = [p.name for p in RESTORE_PHASE_ORDER]
    phases = [
        str(r["phase"])
        for r in ran.events("phase_completed")
        if str(r["phase"]) in order
    ]
    assert phases == sorted(phases, key=order.index)
    # Not vacuous (BL-E003-066): every phase the plan names completed, and no
    # completed phase is missing from the order.
    planned = {str(r["phase"]) for r in ran.events("planned_action")}
    assert planned, "no planned phase"
    assert planned <= set(phases), sorted(planned - set(phases))
    # The Safety Snapshot: the target was populated, so one was taken.
    (written,) = ran.events("safety_snapshot_written")
    snapshot = Path(written["path"])
    assert manifest_origin(snapshot)["restore_mode"] == mode.value
    # JSON lines on stderr (BL-E002-103 UX-5): the WARNING record is the notice.
    assert ran.events("safety_snapshot_unencrypted")
    # The parts the Mode restores changed; the others did not.
    index = (site.root / "index.php").read_bytes()
    assert (index == INDEX) is restores_files(mode)
    assert (site.root / "old.php").exists() is not clears_site_root(mode)
    if restores_database(mode):
        live = _live_database(site)
        assert live != original
        assert "wp_restored" in site.site.checksums(live)
        assert (original in site.site.databases()) is not removes_database(mode)
    else:
        assert _live_database(site) == original
        assert site.site.checksums() == before
    credential_sentinel.add("stdout", ran.out)
    credential_sentinel.add("stderr", ran.err)
    credential_sentinel.assert_clean()


@pytest.mark.parametrize("mode", list(RestoreMode))
def test_a_dry_run_of_each_mode_changes_nothing(
    cli_site: CliSite,  # noqa: F811
    capsys: pytest.CaptureFixture[str],
    mode: RestoreMode,
) -> None:
    site = cli_site
    archive = site.archive()
    before = (tree(site.root), site.site.checksums(), _sha(archive), state_dirs(site))
    databases = site.site.databases()
    ran = run_cli(["--whatIf", *site.argv(archive, *_flags(AUTHORIZED[mode]))], capsys)
    assert ran.code == int(ExitCode.OK), ran.err[-3000:]
    assert ran.out.strip()  # the ordered plan
    after = (tree(site.root), site.site.checksums(), _sha(archive), state_dirs(site))
    assert after == before
    assert site.site.databases() == databases


@pytest.mark.parametrize(
    ("key", "mode"), REFUSING, ids=[render_flags(k) for k, _ in REFUSING]
)
def test_each_refusing_combination_names_its_flag(
    cli_site: CliSite,  # noqa: F811
    capsys: pytest.CaptureFixture[str],
    key: FlagCombination,
    mode: RestoreMode,
) -> None:
    site = cli_site
    before = (tree(site.root), site.site.checksums())
    ran = run_cli(site.argv(site.archive(), *_flags(key)), capsys)
    assert ran.code == int(ExitCode.REFUSED), ran.err[-3000:]
    overrides = dict(ENTRIES)[key][1]
    part = next(p for p in _parts(mode) if not overrides.waives(p))
    assert render_flags(authorizing_combination(mode, part)) in ran.err
    (refused,) = ran.events("restore_refused")
    assert refused["reason"] == "non_empty_target"
    assert (tree(site.root), site.site.checksums()) == before


def test_an_empty_target_takes_its_descriptor_from_the_archive(
    cli_site: CliSite,  # noqa: F811
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    site = cli_site
    empty = tmp_path / "fresh-target"
    empty.mkdir()
    archive = site.archive()
    ran = run_cli(site.argv(archive, "--mode", "all", root=empty), capsys)
    assert ran.code == int(ExitCode.REFUSED)
    assert "ArchiveIdentityUnconfirmedError" in ran.err
    # RT-S02-003: the Archive's claim is not shown, so it cannot be pasted.
    assert site.site.database not in ran.err
    assert "USER@HOST[:PORT]/NAME" in ran.err
    assert not any(empty.iterdir())
    # The Archive's database is the fixture's live one (non-empty), so FR-7
    # needs the table's authorizing flags; the empty directory is the point.
    confirmed = (
        archive,
        *authorizing_flags(RestoreMode.ALL),
        "--archive-database",
        site.identity(),
    )
    # RT-S02-004: the Archive's account already exists on the server, so
    # resetting its password needs the operator's explicit flag.
    ran = run_cli(site.argv(*confirmed, root=empty), capsys)
    assert ran.code == int(ExitCode.REFUSED), ran.err[-3000:]
    assert "ExistingDatabaseUserError" in ran.err
    assert not any(empty.iterdir())
    ran = run_cli(site.argv(*confirmed, "--reset-existing-db-user", root=empty), capsys)
    assert ran.code == int(ExitCode.OK), ran.err[-3000:]
    assert (empty / "index.php").read_bytes() == INDEX
    assert "wp_restored" in site.site.checksums(_live_database_at(empty))


def _live_database_at(root: Path) -> str:
    return str(read_keys(root / "wp-config.php")["DB_NAME"])


def test_a_files_only_v0_archive_with_mode_db_is_refused(
    cli_site: CliSite,  # noqa: F811
    capsys: pytest.CaptureFixture[str],
) -> None:
    site = cli_site
    archive = v0_archive(site.site.work, dump=False, config=None)
    ran = run_cli(site.argv(archive, "--mode", "db", "--force"), capsys)
    assert ran.code == int(ExitCode.REFUSED)
    assert "holds no database dump" in ran.err
    assert "--mode files" in ran.err


def test_overwrite_wp_config_with_a_v0_config(
    cli_site: CliSite,  # noqa: F811
    capsys: pytest.CaptureFixture[str],
) -> None:
    site = cli_site
    marker = "// the v0 archive's own wp-config.php"
    text = config_text(name="source_db", user="source", password="x" * 12, db_host="h")
    archive = v0_archive(site.site.work, dump=True, config=text + marker + "\n")
    before = read_keys(site.site.config)
    ran = run_cli(
        site.argv(archive, "--mode", "files", "--force", "--overwrite-wp-config"),
        capsys,
    )
    assert ran.code == int(ExitCode.OK), ran.err[-3000:]
    assert marker in site.site.config.read_text(encoding="utf-8")
    assert read_keys(site.site.config) == before
    assert host()  # the target still names the CI server
