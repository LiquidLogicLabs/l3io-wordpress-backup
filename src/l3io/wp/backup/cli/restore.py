"""``wpbackup2 restore`` — the flags' translation into the restore vocabulary.

The restore driver is Epic 3's (E003-S02-006 binds it and removes the
subcommand's ``NOT_YET_IMPLEMENTED`` entry). What exists now is the part the
root needs before any Run opens: the flags read into AD-25's
``(RestoreMode, OverrideSet)``, ``SnapshotPolicy`` and ``ConfigPolicy``, and
the parse-time usage checks, so a rejected combination exits ``USAGE`` with
the table's own message whether or not the driver has landed.

``--mode`` is translated through ``cli/scope.py``'s ``CLI_MODE_TO_SCOPE``, the
one place the CLI's ``db`` becomes ``database``.
"""

from __future__ import annotations

from l3io.wp.backup.cli.credentials import admin_spec, wordpress_spec
from l3io.wp.backup.cli.scope import backup_scope_from_cli
from l3io.wp.backup.domain.flag_map import (
    FlagCombination,
    combination,
    resolve_flags,
)
from l3io.wp.backup.domain.restore import (
    ConfigPolicy,
    OverrideSet,
    RestoreMode,
    SnapshotPolicy,
)

__all__ = [
    "check_restore_arguments",
    "config_policy_from_cli",
    "flag_combination",
    "restore_mode_from_cli",
    "snapshot_policy_from_cli",
]


def flag_combination(
    *, mode: str, cleanfirst: bool, drop_database: bool, force: bool
) -> FlagCombination:
    """The AD-25 table key for the restore flags' parsed values."""
    return combination(
        mode=backup_scope_from_cli(mode),
        cleanfirst=cleanfirst,
        drop_database=drop_database,
        force=force,
    )


def restore_mode_from_cli(
    *, mode: str, cleanfirst: bool, drop_database: bool, force: bool
) -> tuple[RestoreMode, OverrideSet]:
    """The Mode and waivers the flags express; raises ``USAGE`` otherwise."""
    return resolve_flags(
        flag_combination(
            mode=mode, cleanfirst=cleanfirst, drop_database=drop_database, force=force
        )
    )


def snapshot_policy_from_cli(*, no_safety_snapshot: bool) -> SnapshotPolicy:
    """``--no-safety-snapshot`` is the explicit opt-out; the default takes one."""
    return SnapshotPolicy.SKIP if no_safety_snapshot else SnapshotPolicy.TAKE


def config_policy_from_cli(*, overwrite_wp_config: bool) -> ConfigPolicy:
    """``--overwrite-wp-config`` may replace the target's ``wp-config.php``."""
    return ConfigPolicy.OVERWRITE if overwrite_wp_config else ConfigPolicy.KEEP


def check_restore_arguments(
    *,
    mode: str,
    cleanfirst: bool,
    drop_database: bool,
    force: bool,
    admin_username: str | None,
    admin_password: str | None,
    admin_aws_secret_id: str | None,
    admin_aws_region: str | None,
    db_aws_secret_id: str | None,
    db_aws_region: str | None,
) -> None:
    """Raise ``UsageError`` for restore arguments no Run could act on.

    The flag combination must express a Restore Mode, and each credential
    flag pair must be whole. Nothing outside the arguments is read.
    """
    restore_mode_from_cli(
        mode=mode, cleanfirst=cleanfirst, drop_database=drop_database, force=force
    )
    admin_spec(
        username=admin_username,
        password=admin_password,
        aws_secret_id=admin_aws_secret_id,
        aws_region=admin_aws_region,
    )
    wordpress_spec(aws_secret_id=db_aws_secret_id, aws_region=db_aws_region)
