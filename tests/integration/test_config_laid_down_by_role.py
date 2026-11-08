"""BL-E007-003 — a SITE_ROOT v1 Archive's config laid down by role, end to end.

``wpbackup2 backup`` of the live Site (its ``wp-config.php`` in the Site Root)
writes the config twice: as the ``config``-role member and in the tree. Then
``wpbackup2 restore`` (the real root, ``drive_restore``) into a fresh,
config-less Site Root over the CI database:

* the config is written exactly once by extraction: the ``config``-role
  extraction lays it down first, and the tree extraction that follows excludes
  ``wp-config.php`` — the file it laid down is the same file, untouched, after
  the tree (same inode, same bytes); one ``config_laid_down`` record;
* the Run Journal records the lay-down's steps in order: ``EXTRACT`` started
  for ``config:<path>``, ``config_started``, ``config``, then ``EXTRACT`` for
  the Site Root.

Needs the CI ``db`` service: skips without ``DB_HOST`` and fails, rather than
skips, when ``CI`` is set.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

import pytest

pytest.importorskip("l3io.wp.database")
pytest.importorskip("pymysql")

from l3io.wp.backup.adapters.journal import journal_directory, record_from_json
from l3io.wp.backup.adapters.site_files import LocalSiteFiles
from l3io.wp.backup.adapters.tar import verify_archive_view
from l3io.wp.backup.core.restore import STEP_CONFIG, STEP_CONFIG_STARTED
from l3io.wp.backup.domain.archive_format import (
    ConfigLocation,
    MemberRole,
)
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.domain.restore import RestoreMode, RestorePhase
from tests.integration.live_cli import (
    CliSite,
    admin_account,
    authorizing_flags,
    cli_site,
    run_cli,
)

__all__ = ["admin_account", "cli_site"]

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _ci_database() -> None:
    """Skip off the Docker CI path; with ``CI`` set a missing database fails."""
    if "DB_HOST" in os.environ:
        return
    if os.environ.get("CI"):
        pytest.fail("DB_HOST is unset and CI is set: the CI database is missing")
    pytest.skip("needs the CI database (DB_HOST unset)")


def _resolved(target: str | None) -> str | None:
    """A journal target with its path resolved (``config:`` prefix kept)."""
    if target is None:
        return None
    prefix = "config:" if target.startswith("config:") else ""
    return prefix + str(Path(target.removeprefix(prefix)).resolve())


def _backup(site: CliSite, capsys: pytest.CaptureFixture[str]) -> Path:
    archive = site.site.work / "by-role.tar.gz"
    ran = run_cli(
        [
            "--log-format",
            "json",
            "backup",
            "--wp-dir",
            str(site.root),
            "--archive",
            str(archive),
        ],
        capsys,
    )
    assert ran.code == int(ExitCode.OK), ran.err[-3000:]
    return archive


def test_a_config_role_lay_down_writes_the_config_once_and_journals_its_steps(
    cli_site: CliSite,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    site = cli_site
    archive = _backup(site, capsys)
    view, _ = verify_archive_view(archive)
    assert view is not None
    assert view.config_path == ConfigLocation.SITE_ROOT.value
    names = {m.path for m in view.members}
    assert {"config/wp-config.php", "site-root/wp-config.php"} <= names
    assert any(m.role is MemberRole.CONFIG for m in view.members)

    target = site.site.work / "fresh"
    target.mkdir()
    config = target / "wp-config.php"
    extractions: list[tuple[list[str], list[str], tuple[int, int, bytes] | None]] = []
    real_extract = LocalSiteFiles.extract

    def extract(
        self: LocalSiteFiles,
        source: object,
        destination: Path,
        view: object,
        *,
        roles: frozenset[MemberRole],
        exclude: frozenset[PurePosixPath] = frozenset(),
        payload: Path | None = None,
    ) -> object:
        result = real_extract(
            self,
            source,  # type: ignore[arg-type]
            destination,
            view,  # type: ignore[arg-type]
            roles=roles,
            exclude=exclude,
            payload=payload,
        )
        if Path(destination).resolve() == target.resolve():
            state = None
            if config.exists():
                info = config.stat()
                state = (info.st_ino, info.st_mtime_ns, config.read_bytes())
            extractions.append(
                (
                    sorted(r.value for r in roles),
                    sorted(str(e) for e in exclude),
                    state,
                )
            )
        return result

    monkeypatch.setattr(LocalSiteFiles, "extract", extract)
    flags = [
        *authorizing_flags(RestoreMode.ALL),
        "--archive-database",
        site.identity(),
        "--reset-existing-db-user",
    ]
    ran = run_cli(site.argv(archive, *flags, root=target), capsys)
    assert ran.code == int(ExitCode.OK), ran.err[-3000:]

    # One write: the config role first, then the tree with the config excluded.
    assert [(roles, exclude) for roles, exclude, _ in extractions] == [
        ([MemberRole.CONFIG.value], []),
        ([MemberRole.SITE_ROOT.value], ["wp-config.php"]),
    ]
    laid, after_tree = extractions[0][2], extractions[1][2]
    assert laid is not None
    assert laid == after_tree, "the tree extraction rewrote the laid-down config"
    assert len(ran.events("config_laid_down")) == 1
    assert (target / "index.php").is_file()

    # The journal's lay-down steps, in order.
    run_ids = {r["l3io_run_id"] for r in ran.records if "l3io_run_id" in r}
    (run_id,) = run_ids
    path = journal_directory(target) / "completed" / f"{run_id}.json"
    record = record_from_json(path.read_text(encoding="utf-8"))
    extract_entries = [
        (e.event, _resolved(e.target), e.step)
        for e in record.entries
        if e.phase is RestorePhase.EXTRACT
    ]
    started_config = ("phase_started", f"config:{config.resolve()}", None)
    expected = [
        started_config,
        ("step_completed", None, STEP_CONFIG_STARTED),
        ("step_completed", None, STEP_CONFIG),
        ("phase_started", str(target.resolve()), None),
    ]
    assert extract_entries[: len(expected)] == expected, extract_entries
