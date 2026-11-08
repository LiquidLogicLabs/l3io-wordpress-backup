"""BL-E003-021 — skipped symlinks through ``wpbackup2 restore``, end to end.

A v1 Archive written by the real writer holds an absolute symlink, one that
escapes the Site Root and one that stays inside it. Restored over the CI
database's live Site in the ``files`` Mode:

* the Run exits ``OK``; one WARNING ``symlink_skipped`` per unsupported link,
  and the plain ``WARNING: symlink ... not restored`` line on stderr; the two
  links are absent on disk, the inside link is restored;
* the links the real extraction skipped are exactly the ones
  ``core.restore.planned_skipped_links`` (the Dry Run's lexical rule) names
  for the same Archive — path, target and reason;
* a ``--whatIf`` of the same restore exits ``OK`` and changes nothing;
* the Safety Snapshot the Run took verifies, and restoring it (``files``,
  ``--cleanfirst``) gives back the Site Root as it was before the Run.

The restore root reads the Site's database (its ``wp-config.php`` names the
throwaway database), so this needs the CI ``db`` service: it skips without
``DB_HOST`` and fails, rather than skips, when ``CI`` is set.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

pytest.importorskip("l3io.wp.database")
pytest.importorskip("pymysql")

from l3io.wp.backup.adapters.tar import (
    collect_site_root,
    verify_archive_view,
    write_archive,
)
from l3io.wp.backup.core.restore import planned_skipped_links
from l3io.wp.backup.domain.archive_format import (
    FORMAT_VERSION,
    PAYLOAD_MEMBER,
    Manifest,
    ManifestMember,
    ManifestPayload,
)
from l3io.wp.backup.domain.exclusion import INCLUDE_EVERYTHING
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.domain.restore import RestoreMode
from tests.integration.live_cli import (
    CliSite,
    admin_account,
    authorizing_flags,
    cli_site,
    run_cli,
    tree,
)

__all__ = ["admin_account", "cli_site"]

pytestmark = pytest.mark.integration

#: The links the Archive carries: name -> target. ``inside`` is supported.
LINKS = {"abs": "/etc/hostname", "up": "../outside", "inside": "index.php"}
SKIPPED = {"abs", "up"}


@pytest.fixture(autouse=True)
def _ci_database() -> None:
    """Skip off the Docker CI path; with ``CI`` set a missing database fails."""
    if "DB_HOST" in os.environ:
        return
    if os.environ.get("CI"):
        pytest.fail("DB_HOST is unset and CI is set: the CI database is missing")
    pytest.skip("needs the CI database (DB_HOST unset)")


def _links_archive(work: Path) -> Path:
    """A files-scope v1 Archive by the real writer, holding :data:`LINKS`."""
    source = work / f"links-{uuid.uuid4().hex[:6]}"
    source.mkdir()
    (source / "index.php").write_text("<?php // with links\n", encoding="utf-8")
    for name, target in LINKS.items():
        (source / name).symlink_to(target)
    members = list(collect_site_root(source, INCLUDE_EVERYTHING).members)

    def manifest(index: tuple[ManifestMember, ...]) -> Manifest:
        return Manifest(
            format_version=FORMAT_VERSION,
            created_at="2026-11-07T00:00:00Z",
            backup_scope="files",
            source={"db_name": "source_db"},
            encrypted=False,
            payload=ManifestPayload(name=PAYLOAD_MEMBER, size=0, sha256="0" * 64),
            members=index,
            dump_present=False,
            config_path=None,
        )

    workspace = work / f"write-{uuid.uuid4().hex[:6]}"
    workspace.mkdir(mode=0o700)
    path = work / f"links-{uuid.uuid4().hex[:6]}.tar.gz"
    with path.open("wb") as out:
        write_archive(out, manifest, members, workspace=workspace, cipher=None)
    return path


def test_unsupported_links_are_skipped_as_planned_and_the_snapshot_round_trips(
    cli_site: CliSite, capsys: pytest.CaptureFixture[str]
) -> None:
    site = cli_site
    archive = _links_archive(site.site.work)
    view, verified = verify_archive_view(archive)
    assert view is not None
    assert verified.passed
    planned = {(s.path, s.link_target, s.reason) for s in planned_skipped_links(view)}
    assert {p for p, _, _ in planned} == SKIPPED

    before = tree(site.root)
    flags = authorizing_flags(RestoreMode.FILES)

    # The Dry Run: refuses nothing, changes nothing.
    dry = run_cli(["--whatIf", *site.argv(archive, *flags)], capsys)
    assert dry.code == int(ExitCode.OK), dry.err[-3000:]
    assert tree(site.root) == before

    # The real Run.
    ran = run_cli(site.argv(archive, *flags), capsys)
    assert ran.code == int(ExitCode.OK), ran.err[-3000:]
    warned = ran.events("symlink_skipped")
    assert len(warned) == len(SKIPPED)
    assert all(r["level"] == "warning" for r in warned)
    assert {r["path"] for r in warned} == {repr(name) for name in SKIPPED}
    extracted = {
        (r["path"].strip("'"), r["link_target"].strip("'"), r["reason"]) for r in warned
    }
    assert extracted == planned
    for name in SKIPPED:
        assert not os.path.lexists(site.root / name), name
        assert f"WARNING: symlink {name!r} -> {LINKS[name]!r} not restored" in ran.err
    assert (site.root / "inside").is_symlink()
    assert str((site.root / "inside").readlink()) == LINKS["inside"]

    # The Safety Snapshot: verifies, and restores the Site Root as it was.
    (written,) = ran.events("safety_snapshot_written")
    snapshot = Path(written["path"])
    checked = run_cli(
        ["--log-format", "json", "verify", "--archive", str(snapshot)], capsys
    )
    assert checked.code == int(ExitCode.OK), checked.err[-3000:]
    back = run_cli(
        site.argv(snapshot, *authorizing_flags(RestoreMode.FILES_CLEAN)), capsys
    )
    assert back.code == int(ExitCode.OK), back.err[-3000:]
    assert back.events("symlink_skipped") == []
    assert tree(site.root) == before
