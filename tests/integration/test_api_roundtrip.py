"""FR-22 — the library entry points against the CI database, beside the CLI.

The live Site is ``tests/integration/live_cli.py``'s ``cli_site``. For each
Backup Scope the API backs the Site up and verifies the result; the CLI backs
the same Site up with the same scope and verifies the API's Archive. Each
API call is one Run (one ``run_id``), and its ``phase_completed`` sequence
equals the CLI's for the same operation. Then the
API restores an Archive it made into the Site, and the CLI restores the same
Archive, with the same Mode and waivers; their phase sequences are equal too.

The restore target is the populated live Site, not an empty one: an empty
target needs a second live database and account, which ``cli_site`` does not
provision, and the populated path is the one both roots share step for step.
"""

from __future__ import annotations

import os
from typing import Any

# Plain imports, not importorskip (BL-E006-002): CI syncs --all-extras, so a
# broken extra must fail this file, not skip it; DB_HOST alone gates the tier.
import l3io.wp.database  # noqa: F401 - the database extra must be installed
import pymysql  # noqa: F401 - the live tier's driver, from that extra
import pytest
from structlog.testing import capture_logs

from l3io.wp.backup import api
from l3io.wp.backup.cli.scope import CLI_MODE_TO_SCOPE
from l3io.wp.backup.domain.exit_codes import ExitCode
from tests.conftest import CredentialSentinel
from tests.integration.live_cli import (
    CliSite,
    Ran,
    admin_account,  # noqa: F401 - a fixture
    authorizing_flags,
    cli_site,  # noqa: F401 - a fixture
    run_cli,
)
from tests.root_mode import allow_root_kwargs

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        "DB_HOST" not in os.environ, reason="needs the CI database (DB_HOST unset)"
    ),
]

SCOPE_TO_CLI = {scope: token for token, scope in CLI_MODE_TO_SCOPE.items()}
ALL_WAIVED = api.OverrideSet(waived=frozenset(api.Override))


def _phases(records: list[dict[str, Any]]) -> list[str]:
    return [str(r["phase"]) for r in records if r.get("event") == "phase_completed"]


def _one_run(records: list[dict[str, Any]]) -> str:
    started = [r for r in records if r.get("event") == "run_started"]
    finished = [r for r in records if r.get("event") == "run_finished"]
    run_ids = {str(r["run_id"]) for r in records if "run_id" in r}
    assert (len(started), len(finished), len(run_ids)) == (1, 1, 1)
    return run_ids.pop()


@pytest.mark.parametrize("scope", list(api.BackupScope))
def test_each_scope_backs_up_and_verifies_as_the_cli_does(
    cli_site: CliSite,  # noqa: F811
    capsys: pytest.CaptureFixture[str],
    credential_sentinel: CredentialSentinel,
    scope: api.BackupScope,
) -> None:
    site = cli_site
    made_at = site.site.work / f"api-{scope.value}.tar.gz"
    with capture_logs() as made:
        result = api.backup(site.root, scope=scope, target=made_at)
    with capture_logs() as checked:
        verified = api.verify(made_at)
    assert result.performed
    assert result.scope is scope
    assert verified.passed
    assert _one_run(made) != _one_run(checked)
    cli: Ran = run_cli(
        [
            "--log-format",
            "json",
            "backup",
            "--wp-dir",
            str(site.root),
            "--archive",
            str(site.site.work / f"cli-{scope.value}.tar.gz"),
            "--mode",
            SCOPE_TO_CLI[scope],
        ],
        capsys,
    )
    assert cli.code == int(ExitCode.OK), cli.err[-3000:]
    assert _phases(made) == _phases(cli.records)
    cli_verify = run_cli(
        ["--log-format", "json", "verify", "--archive", str(made_at)], capsys
    )
    assert cli_verify.code == int(ExitCode.OK), cli_verify.err[-3000:]
    assert _phases(checked) == _phases(cli_verify.records)
    credential_sentinel.add("cli stderr", cli.err)
    credential_sentinel.add("cli verify stderr", cli_verify.err)
    credential_sentinel.assert_clean()


def test_a_restore_of_an_api_backup_runs_the_clis_phases(
    cli_site: CliSite,  # noqa: F811
    capsys: pytest.CaptureFixture[str],
    credential_sentinel: CredentialSentinel,
) -> None:
    site = cli_site
    archive = site.site.work / "roundtrip.tar.gz"
    with capture_logs():
        api.backup(site.root, scope=api.BackupScope.ALL, target=archive)
    with capture_logs() as restored:
        outcome = api.restore(
            site.root,
            archive=archive,
            mode=api.RestoreMode.ALL,
            overrides=ALL_WAIVED,
            snapshot=api.SnapshotPolicy.TAKE,
            config_policy=api.ConfigPolicy.KEEP,
            snapshot_dir=site.snapshots,
            admin=api.ExplicitCredentialSpec(
                username=site.admin_user, password=site.admin_password
            ),
            **allow_root_kwargs(),
        )
    assert _one_run(restored) == outcome.run_id
    assert outcome.database is not None
    assert outcome.database.restored
    cli = run_cli(site.argv(archive, *authorizing_flags(api.RestoreMode.ALL)), capsys)
    assert cli.code == int(ExitCode.OK), cli.err[-3000:]
    assert _phases(restored) == _phases(cli.records)
    credential_sentinel.add("cli stdout", cli.out)
    credential_sentinel.add("cli stderr", cli.err)
    credential_sentinel.assert_clean()
