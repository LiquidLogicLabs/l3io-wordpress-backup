"""E007-S01-002 (AD-18, FR-29) — killed at the cutover, resolved into the parent file.

``wpbackup2 restore`` of a ``PARENT``-config Archive into a fresh
``<tmp>/b/public`` runs in a ``multiprocessing`` **spawn** child whose
``WpConfigWriter.set_constants`` SIGKILLs the child on the first write of
``DB_NAME`` (the cutover; the config itself was laid down before it by
extraction). Then, in this process, ``wpbackup2 restore
--resolve-interrupted-run`` must complete the cutover in
``<tmp>/b/wp-config.php``, the file the journal header names, and never
create ``<tmp>/b/public/wp-config.php`` (the O2 edit to ``cli/restore.py``:
before it, the header named the Site Root's file).

Needs the CI ``db`` service (``DB_HOST``/``DB_PASSWORD``); skips without it.
"""

from __future__ import annotations

import multiprocessing
import os
import signal
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

import pytest

pytest.importorskip("l3io.wp.database")

from l3io.wp.backup.adapters.journal import FileRunJournal, journal_directory
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.domain.restore import RestoreMode
from tests.conftest import CredentialSentinel
from tests.integration.live_cli import (
    CliSite,
    admin_account,
    authorizing_flags,
    cli_site,
    run_cli,
)
from tests.integration.live_restore import live_site, read_keys

__all__ = ["admin_account", "cli_site", "live_site"]

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        "DB_HOST" not in os.environ, reason="needs the CI database (DB_HOST unset)"
    ),
]


def killed_at_the_cutover(argv: list[str], environ: dict[str, str]) -> None:
    """The spawned child: the restore, SIGKILLed as the cutover writes ``DB_NAME``."""
    os.environ.clear()
    os.environ.update(environ)
    from l3io.wp.backup.adapters.config_writer import WpConfigWriter
    from l3io.wp.backup.cli import main

    real = WpConfigWriter.set_constants

    def set_constants(
        self: WpConfigWriter, path: Path, values: Mapping[str, object]
    ) -> object:
        if "DB_NAME" in values:
            os.kill(os.getpid(), signal.SIGKILL)
        return real(self, path, values)  # type: ignore[arg-type]

    WpConfigWriter.set_constants = set_constants  # type: ignore[method-assign]
    main(argv)


def test_a_parent_lay_down_killed_at_the_cutover_resolves_into_the_parent(
    cli_site: CliSite,
    capsys: pytest.CaptureFixture[str],
    credential_sentinel: CredentialSentinel,
) -> None:
    site = cli_site
    work = site.site.work
    source = work / "a" / "public"
    source.mkdir(parents=True)
    (source / "index.php").write_text("<?php // hardened\n", encoding="utf-8")
    (work / "a" / "wp-config.php").write_bytes(site.site.config.read_bytes())
    archive = work / "hardened.tar.gz"
    ran = run_cli(
        ["backup", "--wp-dir", str(source), "--archive", str(archive)], capsys
    )
    assert ran.code == int(ExitCode.OK), ran.err[-3000:]

    root = work / "b" / "public"
    root.mkdir(parents=True)
    argv = site.argv(
        archive,
        *authorizing_flags(RestoreMode.ALL),
        "--archive-database",
        site.identity(),
        "--reset-existing-db-user",
        root=root,
    )
    context = multiprocessing.get_context("spawn")
    child = context.Process(
        target=killed_at_the_cutover, args=(argv, dict(os.environ)), daemon=True
    )
    child.start()
    child.join(300)
    assert child.exitcode == -signal.SIGKILL, child.exitcode

    parent = root.resolve().parent / "wp-config.php"
    assert parent.is_file(), "the lay-down ran before the cutover"
    assert not (root / "wp-config.php").exists()
    canonical = root.resolve()
    record = FileRunJournal(journal_directory(canonical)).incomplete(
        PurePosixPath(canonical)
    )
    assert record is not None
    assert record.header.wp_config_path == str(parent)
    load = record.header.load_database
    assert load is not None
    assert read_keys(parent)["DB_NAME"] != load

    ran = run_cli([*argv, "--resolve-interrupted-run", record.header.run_id], capsys)
    assert ran.code == int(ExitCode.OK), ran.err[-3000:]
    assert read_keys(parent)["DB_NAME"] == load
    assert not (root / "wp-config.php").exists()
    assert (
        FileRunJournal(journal_directory(canonical)).incomplete(
            PurePosixPath(canonical)
        )
        is None
    )
    credential_sentinel.add("resolution output", ran.out + ran.err)
    credential_sentinel.assert_clean()
