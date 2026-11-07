"""E007-S01-002 (FR-29, AD-11) — a Site whose ``wp-config.php`` is one level up.

``wpbackup2 backup`` of ``<tmp>/a/public`` with ``<tmp>/a/wp-config.php``,
then ``wpbackup2 restore`` (the real root, ``drive_restore``) over the CI
database:

1. the Archive lists ``config/wp-config.php``, no name holds ``..``, and the
   Manifest's ``config_path`` is ``"../wp-config.php"``;
2. restored into a fresh ``<tmp>/b/public``: ``<tmp>/b/wp-config.php`` carries
   the cutover's ``DB_NAME``; ``<tmp>/b/public/wp-config.php`` does not exist;
3. ``<tmp>/b/wp-config.php`` already there: only its ``DB_NAME`` line changes;
4. ``<tmp>/b/wp-settings.php`` there: ``REFUSED``, nothing written;
5. a Manifest saying ``../../x``: ``REFUSED``, nothing written;
6. a crafted ``config/../x`` member: the extraction contract
   (``tests/contracts/test_extraction.py``, role-independent) and
   ``tests/unit/adapters/test_wpcli_config_placement.py`` for the ``config``
   role;
7. a database-only Mode with no target config: ``REFUSED``
   (``NoConfigForCutoverError``), the load never ran, the path named.

Also: a v0 Archive lays its config at ``<Site Root>/wp-config.php``; a Dry Run
prints the lay-down and writes nothing; ``--wp-config`` at an existing file is
used, at a missing one is refused, and outside both search places with a URL
relocation is ``USAGE``. ``credential_sentinel`` finds no leak in any record.
The kill-at-cutover case is ``test_config_parent_resolve.py``.

Needs the CI ``db`` service (``DB_HOST``/``DB_PASSWORD``); skips without it.
"""

from __future__ import annotations

import io
import json
import os
import tarfile
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

pytest.importorskip("l3io.wp.database")

from l3io.wp.backup.adapters.tar import verify_archive_view
from l3io.wp.backup.domain.archive_format import (
    MANIFEST_MEMBER,
    ConfigLocation,
    MemberRole,
)
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.domain.restore import (
    RestoreMode,
    restores_database,
    restores_files,
)
from tests.conftest import CredentialSentinel
from tests.integration.live_cli import (
    CliSite,
    admin_account,
    authorizing_flags,
    cli_site,
    run_cli,
    tree,
)
from tests.integration.live_restore import (
    WP_DUMP,
    config_text,
    host,
    live_site,
    read_keys,
)

__all__ = ["admin_account", "cli_site", "live_site"]

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        "DB_HOST" not in os.environ, reason="needs the CI database (DB_HOST unset)"
    ),
]

#: A Mode that restores the database and no files (from the enum).
DATABASE_ONLY = next(
    m for m in RestoreMode if restores_database(m) and not restores_files(m)
)


@dataclass
class Hardened:
    """The CLI Site, its hardened copy ``a/`` and that copy's Archive."""

    site: CliSite
    work: Path
    archive: Path

    def target(self, name: str = "b") -> Path:
        """A fresh ``<work>/<name>/public``."""
        root = self.work / name / "public"
        root.mkdir(parents=True)
        return root

    def flags(self, mode: RestoreMode = RestoreMode.ALL) -> list[str]:
        return [
            *authorizing_flags(mode),
            "--archive-database",
            self.site.identity(),
            "--reset-existing-db-user",
        ]


@pytest.fixture
def hardened(
    cli_site: CliSite,
    capsys: pytest.CaptureFixture[str],
) -> Hardened:
    work = cli_site.site.work
    public = work / "a" / "public"
    public.mkdir(parents=True)
    (public / "index.php").write_text("<?php // hardened\n", encoding="utf-8")
    (work / "a" / "wp-config.php").write_bytes(cli_site.site.config.read_bytes())
    archive = work / "hardened.tar.gz"
    ran = run_cli(
        [
            "--log-format",
            "json",
            "backup",
            "--wp-dir",
            str(public),
            "--archive",
            str(archive),
        ],
        capsys,
    )
    assert ran.code == int(ExitCode.OK), ran.err[-3000:]
    return Hardened(site=cli_site, work=work, archive=archive)


def _v0_wordpress_archive(work: Path, *, config: str) -> Path:
    """A v0 Archive whose ``database.sql`` is WordPress-shaped (``WP_DUMP``).

    ``live_cli.v0_archive``'s Dump is one toy table, enough for a files Mode
    but not for the staging check a database load runs (``wp_options`` with
    ``db_version`` under the config's ``$table_prefix``, ``wp_``).
    """
    path = work / f"v0-wp-{uuid.uuid4().hex[:6]}.tar.gz"
    members = (
        ("wp-root/index.php", b"<?php // v0\n"),
        ("wp-root/wp-config.php", config.encode()),
        ("database.sql", WP_DUMP),
    )
    with tarfile.open(path, "w:gz") as tar:
        top = tarfile.TarInfo("wp-root")
        top.type = tarfile.DIRTYPE
        top.mode = 0o755
        tar.addfile(top)
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o640
            tar.addfile(info, io.BytesIO(data))
    return path


def _with_config_path(source: Path, destination: Path, value: str) -> Path:
    """``source`` with its (plain) outer Manifest's ``config_path`` set to ``value``."""
    with tarfile.open(source) as outer, tarfile.open(destination, "w") as out:
        for info in outer.getmembers():
            reader = outer.extractfile(info)
            assert reader is not None
            data = reader.read()
            if info.name == MANIFEST_MEMBER:
                document = json.loads(data)
                document["config_path"] = value
                data = json.dumps(document).encode()
                info.size = len(data)
            out.addfile(info, io.BytesIO(data))
    return destination


# --------------------------------------------------------------------------
# 1-2: backup and restore one level up
# --------------------------------------------------------------------------


def test_the_archive_carries_the_config_by_role(hardened: Hardened) -> None:
    view, _ = verify_archive_view(hardened.archive)
    assert view is not None
    names = [m.path for m in view.members]
    assert "config/wp-config.php" in names
    assert any(m.role is MemberRole.CONFIG for m in view.members)
    assert not [n for n in names if ".." in n.split("/")]
    assert view.config_path == ConfigLocation.PARENT.value


def test_a_parent_config_is_restored_one_level_up(
    hardened: Hardened,
    capsys: pytest.CaptureFixture[str],
    credential_sentinel: CredentialSentinel,
) -> None:
    root = hardened.target()
    ran = run_cli(
        hardened.site.argv(hardened.archive, *hardened.flags(), root=root), capsys
    )
    assert ran.code == int(ExitCode.OK), ran.err[-3000:]
    laid = root.parent / "wp-config.php"
    assert laid.is_file()
    assert not (root / "wp-config.php").exists()
    assert (root / "index.php").is_file()
    db_name = read_keys(laid)["DB_NAME"]
    assert db_name in hardened.site.site.databases()
    (located,) = ran.events("config_located")
    assert located["config_path"] == str(laid.resolve())
    assert located["action"] == "lay-down"
    credential_sentinel.add("restore output", ran.out + ran.err)
    credential_sentinel.assert_clean()


def test_an_existing_parent_config_changes_only_by_the_cutover(
    hardened: Hardened, capsys: pytest.CaptureFixture[str]
) -> None:
    root = hardened.target()
    existing = root.parent / "wp-config.php"
    before = hardened.site.site.config.read_text(encoding="utf-8")
    before = before.replace("<?php", "<?php\n// e007-s01-002: the target's own", 1)
    existing.write_text(before, encoding="utf-8")
    ran = run_cli(
        hardened.site.argv(
            hardened.archive, *authorizing_flags(RestoreMode.ALL), root=root
        ),
        capsys,
    )
    assert ran.code == int(ExitCode.OK), ran.err[-3000:]
    after = existing.read_text(encoding="utf-8")
    changed = [
        (old, new)
        for old, new in zip(before.splitlines(), after.splitlines(), strict=True)
        if old != new
    ]
    assert len(changed) == 1
    assert "DB_NAME" in changed[0][0]
    assert not (root / "wp-config.php").exists()


# --------------------------------------------------------------------------
# 4, 5, 7: refusals before any effect
# --------------------------------------------------------------------------


def test_a_parent_install_is_refused(
    hardened: Hardened, capsys: pytest.CaptureFixture[str]
) -> None:
    root = hardened.target()
    (root.parent / "wp-settings.php").write_text("<?php\n", encoding="utf-8")
    before = tree(root.parent)
    checksums = hardened.site.site.checksums()
    ran = run_cli(
        hardened.site.argv(hardened.archive, *hardened.flags(), root=root), capsys
    )
    assert ran.code == int(ExitCode.REFUSED), ran.err[-3000:]
    assert "ConfigLocationConflictError" in ran.err
    assert tree(root.parent) == before
    assert hardened.site.site.checksums() == checksums


def test_a_foreign_manifest_location_is_refused(
    hardened: Hardened, capsys: pytest.CaptureFixture[str]
) -> None:
    crafted = _with_config_path(
        hardened.archive, hardened.work / "crafted.tar.gz", "../../x"
    )
    root = hardened.target()
    before = tree(hardened.work / "b")
    ran = run_cli(hardened.site.argv(crafted, *hardened.flags(), root=root), capsys)
    assert ran.code == int(ExitCode.REFUSED), ran.err[-3000:]
    assert "UnsupportedConfigLocationError" in ran.err
    assert tree(hardened.work / "b") == before
    assert not (hardened.work / "x").exists()


def test_a_database_mode_with_no_config_is_refused(
    hardened: Hardened, capsys: pytest.CaptureFixture[str]
) -> None:
    root = hardened.target()
    checksums = hardened.site.site.checksums()
    databases = hardened.site.site.databases()
    ran = run_cli(
        hardened.site.argv(hardened.archive, *hardened.flags(DATABASE_ONLY), root=root),
        capsys,
    )
    assert ran.code == int(ExitCode.REFUSED), ran.err[-3000:]
    assert "NoConfigForCutoverError" in ran.err
    assert str(root.resolve() / "wp-config.php") in ran.err
    assert hardened.site.site.checksums() == checksums
    assert hardened.site.site.databases() == databases
    assert tree(root.parent) == {"public": None}


# --------------------------------------------------------------------------
# v0, the Dry Run and --wp-config
# --------------------------------------------------------------------------


def test_a_v0_archive_uses_the_default_location(
    hardened: Hardened, capsys: pytest.CaptureFixture[str]
) -> None:
    site = hardened.site.site
    archive = _v0_wordpress_archive(
        hardened.work,
        config=config_text(
            name=site.database, user=site.user, password=site.password, db_host=host()
        ),
    )
    root = hardened.target("c")
    ran = run_cli(hardened.site.argv(archive, *hardened.flags(), root=root), capsys)
    assert ran.code == int(ExitCode.OK), ran.err[-3000:]
    assert (root / "wp-config.php").is_file()
    assert not (root.parent / "wp-config.php").exists()
    assert read_keys(root / "wp-config.php")["DB_NAME"] in site.databases()
    (located,) = ran.events("config_located")
    assert located["manifest_config_path"] is None


def test_a_dry_run_prints_the_lay_down_and_writes_nothing(
    hardened: Hardened, capsys: pytest.CaptureFixture[str]
) -> None:
    root = hardened.target()
    argv = hardened.site.argv(hardened.archive, *hardened.flags(), root=root)
    ran = run_cli(["--whatIf", *argv], capsys)
    assert ran.code == int(ExitCode.OK), ran.err[-3000:]
    assert f"lay down {root.resolve().parent / 'wp-config.php'}" in ran.out
    assert tree(root.parent) == {"public": None}


def _stated(hardened: Hardened) -> Path:
    stated = hardened.work / "elsewhere" / "wp-config.php"
    stated.parent.mkdir()
    stated.write_bytes(hardened.site.site.config.read_bytes())
    return stated


def test_a_stated_config_is_used(
    hardened: Hardened, capsys: pytest.CaptureFixture[str]
) -> None:
    stated = _stated(hardened)
    body = stated.read_bytes()
    root = hardened.target()
    ran = run_cli(
        hardened.site.argv(
            hardened.archive,
            *authorizing_flags(RestoreMode.FILES),
            "--wp-config",
            str(stated),
            root=root,
        ),
        capsys,
    )
    assert ran.code == int(ExitCode.OK), ran.err[-3000:]
    assert ran.events("wp_config_outside_search_path")
    assert stated.read_bytes() == body
    assert not (root.parent / "wp-config.php").exists()
    assert not (root / "wp-config.php").exists()


def test_a_missing_stated_config_is_refused(
    hardened: Hardened, capsys: pytest.CaptureFixture[str]
) -> None:
    root = hardened.target()
    missing = hardened.work / "nowhere" / "wp-config.php"
    ran = run_cli(
        hardened.site.argv(
            hardened.archive, *hardened.flags(), "--wp-config", str(missing), root=root
        ),
        capsys,
    )
    assert ran.code == int(ExitCode.FAILED), ran.err[-3000:]
    assert "InvalidSiteDescriptorError" in ran.err
    assert "the stated wp-config.php does not exist" in ran.err
    assert tree(root.parent) == {"public": None}


def test_a_url_relocation_needs_a_config_wordpress_reads(
    hardened: Hardened, capsys: pytest.CaptureFixture[str]
) -> None:
    stated = _stated(hardened)
    root = hardened.target()
    checksums = hardened.site.site.checksums()
    ran = run_cli(
        hardened.site.argv(
            hardened.archive,
            *hardened.flags(),
            "--wp-config",
            str(stated),
            "--new-site-url",
            "https://staging.e007.example",
            root=root,
        ),
        capsys,
    )
    assert ran.code == int(ExitCode.USAGE), ran.err[-3000:]
    assert "is not the file WordPress loads" in ran.err
    assert tree(root.parent) == {"public": None}
    assert hardened.site.site.checksums() == checksums
