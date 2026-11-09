"""Builders shared by the restore plan tests (E003-S01-001). Not a test module."""

from __future__ import annotations

import dataclasses
from pathlib import PurePosixPath

from l3io.wp.backup.core.restore import (
    NO_RELOCATION,
    RestorePlan,
    RestoreRelocation,
    TargetState,
    build_restore_plan,
)
from l3io.wp.backup.domain.archive_format import (
    PAYLOAD_MEMBER,
    ArchiveView,
    Integrity,
    ManifestPayload,
    MemberKind,
    MemberRole,
    ViewMember,
)
from l3io.wp.backup.domain.database import DatabaseLocation, DatabaseTargetState
from l3io.wp.backup.domain.preflight import ToolRequirement
from l3io.wp.backup.domain.restore import (
    ConfigPolicy,
    Override,
    OverrideSet,
    RestoreMode,
    RestorePhase,
    SnapshotPolicy,
)
from l3io.wp.backup.domain.search_replace import UrlRelocation
from l3io.wp.backup.domain.site import SiteDescriptor
from l3io.wp.backup.domain.storage_target import StorageTarget

SITE_ROOT = PurePosixPath("/srv/www")
SNAPSHOTS = PurePosixPath("/var/lib/snapshots")
ARCHIVE = StorageTarget(scheme="file", path=PurePosixPath("/backups/site.tar.gz"))

SITE = SiteDescriptor(
    site_root=SITE_ROOT,
    db_host="db.internal",
    db_port=3307,
    db_name="wordpress",
    table_prefix="wp_",
    site_url=None,
    home_url=None,
    credential_source="wp-config:/srv/www/wp-config.php",
)

FULL_DB = DatabaseTargetState(
    database_exists=True,
    table_count=12,
    is_wordpress=True,
    schema_version="57155",
    user_exists=True,
    routine_count=0,
    event_count=0,
    prefixed_tables=("wp_options", "wp_posts"),
)
EMPTY_DB = dataclasses.replace(
    FULL_DB, table_count=0, is_wordpress=False, prefixed_tables=()
)
#: Owner decision 57: tables, none with the site's prefix (another
#: application's database), so a restore over it needs ``--force``.
FOREIGN_DB = dataclasses.replace(
    FULL_DB, is_wordpress=False, schema_version=None, prefixed_tables=()
)

#: A populated target: files, a database with tables and a config.
POPULATED = TargetState(
    site_root_empty=False,
    database=FULL_DB,
    config_exists=True,
    site_root=SITE_ROOT,
    site_root_entries=3,
    database_location=SITE.database_location(),
    load_target=None,
    load_location=None,
)
#: An empty target: no Site Root entry, no database.
EMPTY = dataclasses.replace(
    POPULATED,
    site_root_empty=True,
    site_root_entries=0,
    database=None,
    config_exists=False,
)

#: Every FR-7 refusal waived (``--force``): the plan-shape tests build plans
#: over :data:`POPULATED`, which the guard would otherwise refuse.
WAIVED = OverrideSet(waived=frozenset(Override))


def member(path: str, role: MemberRole, size: int | None = 10) -> ViewMember:
    return ViewMember(path, role, MemberKind.FILE, None, size)


def view(
    *, dump: bool = True, config: bool = True, encrypted: bool = False
) -> ArchiveView:
    """A v1 view: a Site Root file, a config and (optionally) a Dump."""
    members = [
        ViewMember("site-root", MemberRole.SITE_ROOT, MemberKind.DIR, None, 0),
        member("site-root/index.php", MemberRole.SITE_ROOT, 100),
    ]
    if config:
        members.append(member("site-root/wp-config.php", MemberRole.SITE_ROOT, 50))
    if dump:
        members.append(member("dump/database.sql", MemberRole.DUMP, 1000))
    return ArchiveView(
        format_version=1,
        members=tuple(members),
        dump_present=dump,
        config_path="wp-config.php" if config else None,
        integrity=Integrity.VERIFIED,
        encrypted=encrypted,
        payload=ManifestPayload(name=PAYLOAD_MEMBER, size=500, sha256="0" * 64),
        backup_scope="all" if dump else "files",
    )


#: Relocations built from the type's own fields, not a hand list: each sets
#: the fields a URL-only, a database-only or a combined relocation fills.
URL_ONLY = dataclasses.replace(
    NO_RELOCATION,
    rewrite_values=(
        ("WP_SITEURL", "https://new.test"),
        ("WP_HOME", "https://new.test"),
    ),
    search_replace=UrlRelocation(site_url="https://new.test", home_url=None),
    required_tools=(ToolRequirement("wp-cli", (2, 8), "search-replace"),),
    undo=(
        (RestorePhase.REWRITE_CONFIG, "restore WP_SITEURL/WP_HOME"),
        (RestorePhase.SEARCH_REPLACE, "reload the snapshot's database"),
    ),
)
DATABASE_ONLY = dataclasses.replace(
    NO_RELOCATION,
    load_database="moved_db",
    load_location=DatabaseLocation(host="db2.internal", port=3306, name="moved_db"),
    cutover_host="db2.internal",
    cutover_user="moved_user",
    cutover_password=True,
)
BOTH = dataclasses.replace(
    URL_ONLY,
    **{
        f.name: getattr(DATABASE_ONLY, f.name)
        for f in dataclasses.fields(RestoreRelocation)
        if getattr(DATABASE_ONLY, f.name) != getattr(NO_RELOCATION, f.name)
    },
)
RELOCATIONS = {
    "none": NO_RELOCATION,
    "url-only": URL_ONLY,
    "database-only": DATABASE_ONLY,
    "both": BOTH,
}


def plan(
    mode: RestoreMode,
    *,
    target: TargetState = POPULATED,
    archive_view: ArchiveView | None = None,
    relocation: RestoreRelocation = NO_RELOCATION,
    snapshot: SnapshotPolicy = SnapshotPolicy.TAKE,
    config_policy: ConfigPolicy = ConfigPolicy.KEEP,
    snapshot_dir: PurePosixPath = SNAPSHOTS,
    config_dir: PurePosixPath = SITE_ROOT,
    overrides: OverrideSet = WAIVED,
) -> RestorePlan:
    return build_restore_plan(
        SITE,
        mode,
        overrides,
        archive_view or view(),
        target,
        archive=ARCHIVE,
        relocation=relocation,
        snapshot=snapshot,
        snapshot_dir=snapshot_dir,
        config_dir=config_dir,
        config_policy=config_policy,
    )
