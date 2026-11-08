"""``wpbackup2 restore`` over the CI database, for E003-S02-006's integration tests.

Not a test module. :func:`cli_site` is a live Site — a Site Root with an old
file and a ``wp-config.php`` naming a uniquely named, seeded throwaway
database, logged into as ``live_account`` (password: the sentinel) — and
:func:`admin_account` a throwaway administrator ``WITH GRANT OPTION`` whose
password is ``credential_sentinel.admin_password``. Teardown drops that
database, every database whose name starts with it (staging, snapshots'
loads) and the administrator, through CI's own connection only.

:func:`run_cli` drives ``main`` in process with ``--log-format json`` and
returns the exit code, stdout, stderr and the parsed JSON records.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from l3io.wp.backup.adapters.journal import journal_directory
from l3io.wp.backup.cli import main
from l3io.wp.backup.cli.restore import render_flags
from l3io.wp.backup.domain.flag_map import FLAG_MAP, FlagRejection
from l3io.wp.backup.domain.restore import RestoreMode, restores_database, restores_files
from tests.conftest import CredentialSentinel
from tests.integration.live_restore import (
    LiveSite,
    build_archive,
    config_text,
    host,
    port,
    root_connection,
)
from tests.root_mode import allow_root_argv


@dataclass
class CliSite:
    """A live Site, its administrator and where its snapshots go."""

    site: LiveSite
    admin_user: str
    admin_password: str = field(repr=False)

    @property
    def root(self) -> Path:
        return self.site.site_root

    @property
    def snapshots(self) -> Path:
        return self.site.work / "snapshots"

    def archive(self) -> Path:
        """A v1 Archive by the real writer: index, an in-tree config, the Dump."""
        return build_archive(
            self.site.work,
            config=config_text(
                name=self.site.database,
                user=self.site.user,
                password=self.site.password,
                db_host=host(),
            ),
        )

    def identity(self) -> str:
        """``--archive-database`` naming this Site's database exactly."""
        return f"{self.site.user}@{host()}:{port()}/{self.site.database}"

    def argv(self, archive: Path, *flags: str, root: Path | None = None) -> list[str]:
        return [
            "--log-format",
            "json",
            "restore",
            "--wp-dir",
            str(root or self.root),
            "--archive",
            str(archive),
            "--snapshot-dir",
            str(self.snapshots),
            "--admin-username",
            self.admin_user,
            "--admin-password",
            self.admin_password,
            *allow_root_argv(),
            *flags,
        ]


@dataclass
class Ran:
    code: int
    out: str
    err: str
    records: list[dict[str, Any]]

    def events(self, name: str) -> list[dict[str, Any]]:
        return [r for r in self.records if r.get("event") == name]


def run_cli(argv: Sequence[str], capsys: pytest.CaptureFixture[str]) -> Ran:
    capsys.readouterr()
    code = main(list(argv))
    captured = capsys.readouterr()
    records = [
        json.loads(line) for line in captured.err.splitlines() if line.startswith("{")
    ]
    return Ran(code=code, out=captured.out, err=captured.err, records=records)


@pytest.fixture
def admin_account(credential_sentinel: CredentialSentinel) -> Iterator[str]:
    """A throwaway administrator (``WITH GRANT OPTION``), sentinel password."""
    user = f"wpa_{uuid.uuid4().hex[:12]}"
    with root_connection().cursor() as cursor:
        cursor.execute(
            "CREATE USER %s@'%%' IDENTIFIED BY %s",
            (user, credential_sentinel.admin_password),
        )
        cursor.execute(
            "GRANT ALL PRIVILEGES ON *.* TO %s@'%%' WITH GRANT OPTION", (user,)
        )
    try:
        yield user
    finally:
        with root_connection().cursor() as cursor:
            cursor.execute("DROP USER IF EXISTS %s@'%%'", (user,))


@pytest.fixture
def cli_site(
    credential_sentinel: CredentialSentinel, live_account: str, admin_account: str
) -> Iterator[CliSite]:
    s = credential_sentinel
    (s.site / "old.php").write_text("<?php\n// old\n", encoding="utf-8")
    site = LiveSite(
        site_root=s.site,
        config=s.config,
        database=f"wpc_{uuid.uuid4().hex[:10]}",
        user=live_account,
        password=s.password,
        work=s.workdir,
    )
    site.seed()
    site.write_config()
    try:
        yield CliSite(
            site=site, admin_user=admin_account, admin_password=s.admin_password
        )
    finally:
        site.teardown()


def authorizing_flags(mode: RestoreMode) -> list[str]:
    """The table's flags that select ``mode`` and waive every part it restores."""
    parts = [
        part
        for part, touched in (
            ("files", restores_files(mode)),
            ("database", restores_database(mode)),
        )
        if touched
    ]
    for key, entry in FLAG_MAP.items():
        if isinstance(entry, FlagRejection) or entry[0] is not mode:
            continue
        if all(entry[1].waives(part) for part in parts):  # type: ignore[arg-type]
            return render_flags(key).split()
    msg = f"no FLAG_MAP row authorizes {mode.value}"
    raise AssertionError(msg)


def tree(root: Path) -> dict[str, str | None]:
    """Every entry under ``root``: a file's SHA-256, ``None`` for a directory."""
    found: dict[str, str | None] = {}
    if not root.exists():
        return found
    for directory, dirs, files in os.walk(root):
        for name in dirs:
            found[os.path.relpath(Path(directory) / name, root)] = None
        for name in files:
            path = Path(directory) / name
            found[os.path.relpath(path, root)] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return found


def state_dirs(site: CliSite) -> dict[str, dict[str, str | None]]:
    """The snapshot and journal directories, as trees."""
    return {
        "snapshots": tree(site.snapshots),
        "journal": tree(journal_directory(site.root)),
    }


def manifest_origin(snapshot: Path) -> dict[str, Any]:
    """The Safety Snapshot's Manifest ``origin``."""
    with tarfile.open(snapshot) as outer:
        first = outer.getmembers()[0]
        handle = outer.extractfile(first)
        assert handle is not None
        origin: dict[str, Any] = json.loads(handle.read())["origin"]
        return origin


def v0_archive(work: Path, *, dump: bool, config: str | None) -> Path:
    """A v0 Archive: ``wp-root/`` (with ``wp-config.php`` if given), maybe a Dump."""
    path = work / f"v0-{uuid.uuid4().hex[:6]}.tar.gz"
    members: list[tuple[str, bytes]] = [("wp-root/index.php", b"<?php // v0\n")]
    if config is not None:
        members.append(("wp-root/wp-config.php", config.encode()))
    if dump:
        members.append(("database.sql", b"CREATE TABLE `wp_v0` (`id` int);\n"))
    with tarfile.open(path, "w:gz") as tar:
        root = tarfile.TarInfo("wp-root")
        root.type = tarfile.DIRTYPE
        root.mode = 0o755
        tar.addfile(root)
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return path


__all__ = [
    "CliSite",
    "Ran",
    "admin_account",
    "authorizing_flags",
    "cli_site",
    "manifest_origin",
    "run_cli",
    "state_dirs",
    "tree",
    "v0_archive",
]
