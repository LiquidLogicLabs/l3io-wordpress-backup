"""FR-4/AD-26 (E008-S01-001, Gate E004 AG-10) -- encrypted, CLI to CLI.

``wpbackup2 backup --encrypt-with-passphrase`` writes an Archive of a live
Site; ``wpbackup2 restore`` of it, through ``cli.main`` over the CI database:

* the same passphrase restores it: the Site Root tree (``wp-config.php``
  aside, which the cutover may rewrite) and the live database's tables equal
  what was backed up;
* a wrong passphrase exits ``VERIFICATION_FAILED`` (8,
  ``PassphraseRejectedError``) with no Safety Snapshot written and the Site
  unchanged;
* no passphrase exits ``REFUSED`` (``PassphraseRequiredError``) before
  ``VERIFY``, with the Site unchanged.

The ``drive_restore`` cases with the real cipher are
``test_encrypted_backup.py``'s. The passphrase is ``credential_sentinel``'s
own (``{"passphrase": True}``): planted in ``L3IO_WP_BACKUP_PASSPHRASE``
before the fixture records ``os.environ``, so ``assert_clean`` sees a Run
that left the environment as it found it (owner decision O3 (a)), and the
passphrase is one of the secrets checked for leaks. Needs the CI database;
skips when ``DB_HOST`` is unset.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

pytest.importorskip("l3io.wp.database")
pytest.importorskip("pymysql")

from l3io.wp.backup.cli.passphrase import PASSPHRASE_ENV
from l3io.wp.backup.domain.errors import PassphraseRequiredError
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.domain.restore import RestoreMode
from tests.conftest import CredentialSentinel
from tests.integration.live_cli import (
    CliSite,
    admin_account,
    authorizing_flags,
    cli_site,
    run_cli,
    tree,
)
from tests.integration.live_restore import live_site, read_keys

__all__ = ["admin_account", "cli_site", "live_site"]

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        "DB_HOST" not in os.environ, reason="needs the CI database (DB_HOST unset)"
    ),
    pytest.mark.parametrize(
        "credential_sentinel", [{"passphrase": True}], indirect=True
    ),
]

MODE = RestoreMode.ALL_CLEAN


def _backup(site: CliSite, passphrase: str, capsys: pytest.CaptureFixture[str]) -> Path:
    """The backup, with ``passphrase`` already in the environment."""
    assert os.environ.get(PASSPHRASE_ENV) == passphrase
    archive = site.site.work / f"encrypted-{uuid.uuid4().hex[:8]}.tar.gz"
    ran = run_cli(
        [
            "--log-format",
            "json",
            "backup",
            "--wp-dir",
            str(site.root),
            "--archive",
            str(archive),
            "--mode",
            "all",
            "--encrypt-with-passphrase",
        ],
        capsys,
    )
    assert ran.code == int(ExitCode.OK), ran.err[-3000:]
    assert archive.exists()
    assert passphrase not in ran.out + ran.err
    return archive


def _site_tree(site: CliSite) -> dict[str, str | None]:
    found = tree(site.root)
    found.pop("wp-config.php", None)
    return found


def _live(site: CliSite) -> str:
    return str(read_keys(site.site.config)["DB_NAME"])


def _state(site: CliSite) -> tuple[object, ...]:
    return (
        tree(site.root),
        site.site.checksums(_live(site)),
        site.site.databases(),
    )


@pytest.fixture
def encrypted(
    cli_site: CliSite,
    credential_sentinel: CredentialSentinel,
    capsys: pytest.CaptureFixture[str],
) -> tuple[CliSite, Path, str]:
    passphrase = credential_sentinel.passphrase
    assert passphrase is not None
    archive = _backup(cli_site, passphrase, capsys)
    return cli_site, archive, passphrase


def test_the_same_passphrase_restores_tree_and_database(
    encrypted: tuple[CliSite, Path, str],
    credential_sentinel: CredentialSentinel,
    capsys: pytest.CaptureFixture[str],
) -> None:
    site, archive, passphrase = encrypted
    files = _site_tree(site)
    tables = site.site.checksums(_live(site))
    (site.root / "added-after-backup.php").write_text("<?php\n", encoding="utf-8")
    (site.root / "old.php").unlink()
    assert os.environ.get(PASSPHRASE_ENV) == passphrase
    ran = run_cli(site.argv(archive, *authorizing_flags(MODE)), capsys)
    assert ran.code == int(ExitCode.OK), ran.err[-3000:]
    assert _site_tree(site) == files
    assert site.site.checksums(_live(site)) == tables
    assert "safety snapshot is encrypted with the passphrase" in ran.err
    credential_sentinel.add("stdout", ran.out)
    credential_sentinel.add("stderr", ran.err)
    credential_sentinel.assert_clean()


def test_a_wrong_passphrase_exits_8_with_nothing_changed(
    encrypted: tuple[CliSite, Path, str],
    credential_sentinel: CredentialSentinel,
    capsys: pytest.CaptureFixture[str],
) -> None:
    site, archive, passphrase = encrypted
    before = _state(site)
    snapshots = tree(site.snapshots)
    wrong = f"wrong-{passphrase}"
    credential_sentinel.monkeypatch.setenv(PASSPHRASE_ENV, wrong)
    ran = run_cli(site.argv(archive, *authorizing_flags(MODE)), capsys)
    assert ran.code == int(ExitCode.VERIFICATION_FAILED) == 8, ran.err[-3000:]
    (failed,) = ran.events("command_failed")
    assert failed["error"] == "PassphraseRejectedError"
    assert tree(site.snapshots) == snapshots, "a Safety Snapshot was written"
    assert not ran.events("safety_snapshot_written")
    assert _state(site) == before
    assert wrong not in ran.out + ran.err
    assert passphrase not in ran.out + ran.err


def test_no_passphrase_is_refused_before_verify(
    encrypted: tuple[CliSite, Path, str],
    credential_sentinel: CredentialSentinel,
    capsys: pytest.CaptureFixture[str],
) -> None:
    site, archive, _ = encrypted
    before = _state(site)
    credential_sentinel.monkeypatch.delenv(PASSPHRASE_ENV, raising=False)
    ran = run_cli(site.argv(archive, *authorizing_flags(MODE)), capsys)
    assert ran.code == int(PassphraseRequiredError.exit_code), ran.err[-3000:]
    assert PassphraseRequiredError.exit_code is ExitCode.REFUSED
    (failed,) = ran.events("command_failed")
    assert failed["error"] == "PassphraseRequiredError"
    phases = {str(r.get("phase")) for r in ran.events("phase_started")}
    assert "VERIFY" not in phases
    assert _state(site) == before
