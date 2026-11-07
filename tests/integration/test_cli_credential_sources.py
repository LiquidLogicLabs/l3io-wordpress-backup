"""FR-14 (E008-S01-001, the CLI cases moved from E005-S01-001) -- sources by flag.

``wpbackup2 backup --mode db`` through ``cli.main`` over the CI database, the
account being ``live_account``'s throwaway user whose password is the
sentinel. ``wp-config.php`` holds a wrong password, so a Run that succeeds
proves the other source answered:

* ``--db-credentials-aws-secret-id``/``--db-credentials-aws-region``: the
  account comes from AWS Secrets Manager (``boto3.client`` patched to a fake
  ``secretsmanager`` client returning the account as JSON), and
  ``credential_source_selected`` names ``aws-secrets-manager:<id>``;
* ``L3IO_WP_BACKUP_DB_USER``/``L3IO_WP_BACKUP_DB_PASSWORD``: the environment
  wins, and ``credential_source_selected`` carries the environment label.

``drive_backup``'s library cases are ``test_credential_sources.py``'s. Needs
the CI database; skips when ``DB_HOST`` is unset.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

pytest.importorskip("l3io.wp.database")

from l3io.wp.backup.adapters.credential_providers import (
    WORDPRESS_PASSWORD_VAR,
    WORDPRESS_USER_VAR,
)
from l3io.wp.backup.domain.exit_codes import ExitCode
from tests.conftest import CredentialSentinel, write_wp_config
from tests.integration.live_cli import run_cli

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        "DB_HOST" not in os.environ, reason="needs the CI database (DB_HOST unset)"
    ),
    pytest.mark.usefixtures("live_account"),
]

SECRET_ID = f"e008/wordpress-db-{uuid.uuid4().hex[:8]}"
REGION = "eu-west-1"
AWS_LABEL = f"aws-secrets-manager:{SECRET_ID}"
ENV_LABEL = f"environment:{WORDPRESS_USER_VAR},{WORDPRESS_PASSWORD_VAR}"


class _SecretsManager:
    """A ``secretsmanager`` client: one secret, every call answered."""

    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.asked: list[str] = []

    def get_secret_value(self, *, SecretId: str) -> dict[str, str]:
        self.asked.append(SecretId)
        return {"SecretString": self.payload, "Name": SecretId}


def _argv(s: CredentialSentinel, *extra: str) -> tuple[list[str], Path]:
    target = s.workdir / f"cli-{uuid.uuid4().hex}.tar.gz"
    argv = [
        "--log-format",
        "json",
        "backup",
        "--wp-dir",
        str(s.site),
        "--archive",
        str(target),
        "--mode",
        "db",
        *extra,
    ]
    return argv, target


def _selected(records: list[dict[str, object]]) -> set[str]:
    return {
        str(r["credential_source"])
        for r in records
        if r.get("event") == "credential_source_selected"
    }


def test_an_aws_secret_selected_by_flag_answers(
    credential_sentinel: CredentialSentinel,
    live_account: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    boto3 = pytest.importorskip("boto3")
    s = credential_sentinel
    write_wp_config(s.config, password=f"wrong-{uuid.uuid4().hex}", user=live_account)
    client = _SecretsManager(
        json.dumps({"username": live_account, "password": s.password})
    )
    regions: list[object] = []

    def make_client(service: str, **kwargs: object) -> _SecretsManager:
        assert service == "secretsmanager"
        regions.append(kwargs.get("region_name"))
        return client

    monkeypatch.setattr(boto3, "client", make_client)
    argv, target = _argv(
        s,
        "--db-credentials-aws-secret-id",
        SECRET_ID,
        "--db-credentials-aws-region",
        REGION,
    )
    ran = run_cli(argv, capsys)
    assert ran.code == int(ExitCode.OK), ran.err[-3000:]
    assert target.exists()
    assert set(client.asked) == {SECRET_ID}
    assert set(regions) == {REGION}
    assert _selected(ran.records) == {AWS_LABEL}
    s.add("stdout", ran.out)
    s.add("stderr", ran.err)
    s.assert_clean()


def test_the_environment_wins_at_the_cli(
    credential_sentinel: CredentialSentinel,
    live_account: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    s = credential_sentinel
    write_wp_config(s.config, password=f"wrong-{uuid.uuid4().hex}", user=live_account)
    s.monkeypatch.setenv(WORDPRESS_USER_VAR, live_account)
    s.monkeypatch.setenv(WORDPRESS_PASSWORD_VAR, s.password)
    argv, target = _argv(s)
    ran = run_cli(argv, capsys)
    assert ran.code == int(ExitCode.OK), ran.err[-3000:]
    assert target.exists()
    assert _selected(ran.records) == {ENV_LABEL}
    s.monkeypatch.delenv(WORDPRESS_USER_VAR)
    s.monkeypatch.delenv(WORDPRESS_PASSWORD_VAR)
    s.add("stdout", ran.out)
    s.add("stderr", ran.err)
    s.assert_clean()
