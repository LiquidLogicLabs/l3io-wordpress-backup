"""``cli/restore.py`` — the restore flags read into the domain's vocabulary."""

from __future__ import annotations

from pathlib import Path

import pytest

from l3io.wp.backup.cli.restore_request import (
    check_restore_arguments,
    config_policy_from_cli,
    flag_combination,
    restore_mode_from_cli,
    snapshot_policy_from_cli,
)
from l3io.wp.backup.cli.scope import CLI_MODE_TO_SCOPE
from l3io.wp.backup.domain.errors import FlagCombinationRejectedError, UsageError
from l3io.wp.backup.domain.flag_map import FLAG_MAP
from l3io.wp.backup.domain.restore import (
    ConfigPolicy,
    OverrideSet,
    RestoreMode,
    SnapshotPolicy,
)

NO_CREDENTIALS: dict[str, str | None] = {
    "admin_username": None,
    "admin_password": None,
    "admin_aws_secret_id": None,
    "admin_aws_region": None,
    "db_aws_secret_id": None,
    "db_aws_region": None,
}
DEFAULT_FLAGS = {"cleanfirst": False, "drop_database": False, "force": False}


@pytest.mark.parametrize("mode", sorted(CLI_MODE_TO_SCOPE))
def test_every_cli_mode_spelling_is_a_table_key(mode: str) -> None:
    assert flag_combination(mode=mode, **DEFAULT_FLAGS) in FLAG_MAP


def test_the_default_restore_is_all_with_nothing_waived() -> None:
    assert restore_mode_from_cli(mode="all", **DEFAULT_FLAGS) == (
        RestoreMode.ALL,
        OverrideSet.none(),
    )


def test_the_policies_default_to_the_safe_value() -> None:
    assert snapshot_policy_from_cli(no_safety_snapshot=False) is SnapshotPolicy.TAKE
    assert snapshot_policy_from_cli(no_safety_snapshot=True) is SnapshotPolicy.SKIP
    assert config_policy_from_cli(overwrite_wp_config=False) is ConfigPolicy.KEEP
    assert config_policy_from_cli(overwrite_wp_config=True) is ConfigPolicy.OVERWRITE


def test_a_rejected_combination_is_a_usage_error() -> None:
    with pytest.raises(FlagCombinationRejectedError):
        check_restore_arguments(
            mode="db", **(DEFAULT_FLAGS | {"cleanfirst": True}), **NO_CREDENTIALS
        )


@pytest.mark.parametrize(
    "credentials",
    [
        {"admin_username": "root"},
        {"admin_password": "x"},
        {"admin_aws_region": "eu-west-1"},
        {"db_aws_region": "eu-west-1"},
    ],
    ids=["lone-username", "lone-password", "admin-region", "db-region"],
)
def test_an_incomplete_credential_pair_is_a_usage_error(
    credentials: dict[str, str],
) -> None:
    with pytest.raises(UsageError):
        check_restore_arguments(
            mode="all", **DEFAULT_FLAGS, **(NO_CREDENTIALS | credentials)
        )


def test_whole_arguments_pass() -> None:
    check_restore_arguments(mode="all", **DEFAULT_FLAGS, **NO_CREDENTIALS)


# Defect 3 (sidecar test, 2026-11-08): README's environment pair, no flag.
ADMIN_ENV = {
    "L3IO_WP_BACKUP_ADMIN_USER": "root",
    "L3IO_WP_BACKUP_ADMIN_PASSWORD": "admin-password-value",
}


@pytest.mark.parametrize("mode", ["files", "db", "all"])
@pytest.mark.parametrize(
    "environ",
    [ADMIN_ENV, {"L3IO_WP_BACKUP_ADMIN_PASSWORD": "admin-password-value"}],
    ids=["pair", "password-only"],
)
def test_the_administrator_variables_alone_are_no_usage_error(
    mode: str, environ: dict[str, str]
) -> None:
    check_restore_arguments(
        mode=mode, **DEFAULT_FLAGS, **NO_CREDENTIALS, environ=environ
    )


def test_the_cli_takes_the_environment_pair_without_admin_username(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``main`` gets past the argument check: the Run answers (no Archive)."""
    from l3io.wp.backup.cli import main
    from l3io.wp.backup.domain.exit_codes import ExitCode

    for name, value in ADMIN_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    site = tmp_path / "site"
    site.mkdir()
    code = main(
        [
            "restore",
            "--wp-dir",
            str(site),
            "--archive",
            str(tmp_path / "absent.tar.gz"),
            "--mode",
            "files",
        ]
    )
    err = capsys.readouterr().err
    assert "--admin-username" not in err
    assert "ArchiveNotFoundError" in err
    assert code == ExitCode.USAGE  # the absent Archive's own code
