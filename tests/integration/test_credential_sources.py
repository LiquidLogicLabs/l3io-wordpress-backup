"""FR-14 — the WordPress account's sources, through ``drive_backup``.

The library path: ``drive_backup(BackupRequest(..., credentials=None))``
reaches ``adapters/credential_providers.wordpress_credentials`` through
``cli/credentials.wordpress_source`` → ``adapters/database.wordpress_source``
with ``os.environ``. The account is ``live_account``'s throwaway user, whose
password is the sentinel. Needs the CI database; skips when ``DB_HOST`` is
unset. A CLI run selecting AWS by flag is E008-S01-001's.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

pytest.importorskip("l3io.wp.database")

from l3io.wp.backup.adapters.credential_providers import (
    WORDPRESS_PASSWORD_VAR,
    WORDPRESS_USER_VAR,
)
from l3io.wp.backup.cli import backup as backup_cli
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.errors import CredentialsError
from l3io.wp.backup.domain.exit_codes import ExitCode
from tests.conftest import CredentialSentinel, write_wp_config

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        "DB_HOST" not in os.environ, reason="needs the CI database (DB_HOST unset)"
    ),
    pytest.mark.usefixtures("live_account"),
]

ENV_LABEL = f"environment:{WORDPRESS_USER_VAR},{WORDPRESS_PASSWORD_VAR}"


def _drive(sentinel: CredentialSentinel) -> Path:
    target = sentinel.workdir / f"driven-{uuid.uuid4().hex}.tar.gz"
    request = backup_cli.BackupRequest(
        site_root=sentinel.site,
        scope=BackupScope.DATABASE,
        target=backup_cli.storage_target_from_cli(str(target)),
        wp_config=None,
        credentials=None,
        cipher=None,
    )
    backup_cli.drive_backup(request, ctx=None, secrets=backup_cli.SecretRegistry())
    return target


def _selected(sentinel: CredentialSentinel) -> set[str]:
    return {
        str(e["credential_source"])
        for e in sentinel.logs
        if e.get("event") == "credential_source_selected"
    }


def test_the_environment_wins_over_a_wrong_wp_config(
    credential_sentinel: CredentialSentinel, live_account: str
) -> None:
    s = credential_sentinel
    write_wp_config(s.config, password=f"wrong-{uuid.uuid4().hex}", user=live_account)
    s.monkeypatch.setenv(WORDPRESS_USER_VAR, live_account)
    s.monkeypatch.setenv(WORDPRESS_PASSWORD_VAR, s.password)
    archive = _drive(s)
    assert archive.exists()
    assert _selected(s) == {ENV_LABEL}
    s.monkeypatch.delenv(WORDPRESS_USER_VAR)
    s.monkeypatch.delenv(WORDPRESS_PASSWORD_VAR)
    s.assert_clean()


def test_wp_config_answers_when_the_environment_is_unset(
    credential_sentinel: CredentialSentinel,
) -> None:
    s = credential_sentinel
    archive = _drive(s)
    assert archive.exists()
    assert _selected(s) == {f"wp-config:{s.config.resolve()}"}
    s.assert_clean()


def test_a_half_set_pair_is_credentials_not_wp_config(
    credential_sentinel: CredentialSentinel, live_account: str
) -> None:
    s = credential_sentinel
    s.monkeypatch.setenv(WORDPRESS_USER_VAR, live_account)
    with pytest.raises(CredentialsError) as caught:
        _drive(s)
    assert caught.value.exit_code == ExitCode.CREDENTIALS
    assert caught.value.missing_key == WORDPRESS_PASSWORD_VAR
    assert _selected(s) == set()
    s.monkeypatch.delenv(WORDPRESS_USER_VAR)
    s.add_exception("backup driver", caught.value)
    s.assert_clean()
