"""AD-3 — both Site Descriptor constructors, against ``tests/data/wp-config.php``."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path, PurePosixPath

import pytest

pytest.importorskip("l3io.wp.database")

from l3io.wp.backup.adapters.database import (
    require_wp_config_credentials,
    site_descriptor_from_archive_config,
    site_descriptor_from_wp_config,
)
from l3io.wp.backup.domain.archive_format import (
    ENCRYPTED_PAYLOAD_MEMBER,
    FORMAT_VERSION,
    Manifest,
    ManifestExclusions,
    ManifestMember,
    ManifestOrigin,
    ManifestPayload,
    MemberKind,
    MemberRole,
    OriginKind,
    manifest_to_json,
    source_from_descriptor,
)
from l3io.wp.backup.domain.errors import (
    CredentialsError,
    InvalidSiteDescriptorError,
    MissingExtraError,
)
from l3io.wp.backup.domain.exclusion import ExcludedEntry

FIXTURE = Path(__file__).resolve().parents[1] / "data" / "wp-config.php"
#: The DB_PASSWORD planted in the fixture; searched for, never printed.
PLANTED_PASSWORD = "pass@word1"


def _config(tmp_path: Path, body: str, name: str = "wp-config.php") -> Path:
    path = tmp_path / name
    path.write_text("<?php\n" + body, encoding="utf-8")
    return path


def test_the_live_constructor_reads_the_fixture(tmp_path: Path) -> None:
    descriptor, credentials = site_descriptor_from_wp_config(
        tmp_path, config_file=FIXTURE
    )
    assert descriptor.site_root == PurePosixPath(tmp_path.resolve())
    assert (descriptor.db_host, descriptor.db_port) == ("wordpressdb", 3306)
    assert descriptor.db_name == "wordpress"
    assert descriptor.table_prefix == "wp_"
    assert descriptor.site_url is None
    assert descriptor.home_url is None
    assert descriptor.credential_source == f"wp-config:{FIXTURE}"
    assert credentials.source == descriptor.credential_source
    assert PLANTED_PASSWORD not in repr(descriptor)
    assert PLANTED_PASSWORD not in repr(credentials)


def test_the_archive_constructor_points_at_the_target(tmp_path: Path) -> None:
    workspace = tmp_path / "run" / "config"
    workspace.mkdir(parents=True)
    extracted = workspace / "wp-config.php"
    shutil.copyfile(FIXTURE, extracted)
    target = tmp_path / "target-site"  # need not exist yet
    descriptor, _ = site_descriptor_from_archive_config(target, config_file=extracted)
    assert descriptor.site_root == PurePosixPath(target)
    assert descriptor.db_name == "wordpress"
    live, _ = site_descriptor_from_wp_config(tmp_path, config_file=FIXTURE)
    assert descriptor.db_host == live.db_host
    assert descriptor.table_prefix == live.table_prefix


def test_host_port_prefix_and_urls_are_read(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        "define('DB_NAME', 'site');\ndefine('DB_USER', 'u');\n"
        "define('DB_PASSWORD', 'p');\ndefine('DB_HOST', 'db.internal:3307');\n"
        "define('WP_SITEURL', 'https://example.test/wp');\n"
        "define('WP_HOME', 'https://example.test');\n"
        "$table_prefix = 'xy_';\n",
    )
    descriptor, _ = site_descriptor_from_wp_config(tmp_path, config_file=config)
    assert (descriptor.db_host, descriptor.db_port) == ("db.internal", 3307)
    assert descriptor.table_prefix == "xy_"
    assert descriptor.site_url == "https://example.test/wp"
    assert descriptor.home_url == "https://example.test"


def test_a_missing_file_is_an_invalid_site(tmp_path: Path) -> None:
    with pytest.raises(InvalidSiteDescriptorError):
        site_descriptor_from_wp_config(tmp_path, config_file=tmp_path / "nope.php")
    with pytest.raises(InvalidSiteDescriptorError) as caught:
        site_descriptor_from_archive_config(tmp_path, config_file=None)
    assert caught.value.reason == "archive has no wp-config.php"
    with pytest.raises(InvalidSiteDescriptorError):
        site_descriptor_from_archive_config(tmp_path, config_file=tmp_path / "nope.php")


def test_a_missing_site_root_is_an_invalid_site(tmp_path: Path) -> None:
    with pytest.raises(InvalidSiteDescriptorError):
        site_descriptor_from_wp_config(tmp_path / "absent", config_file=FIXTURE)


@pytest.mark.parametrize(
    ("defines", "missing"),
    [
        ("define('DB_USER', 'u');\n", "DB_PASSWORD"),
        ("define('DB_PASSWORD', 'never-shown');\n", "DB_USER"),
    ],
)
def test_half_a_credential_pair_names_the_key_not_the_value(
    tmp_path: Path, defines: str, missing: str
) -> None:
    config = _config(
        tmp_path, "define('DB_NAME', 'site');\ndefine('DB_HOST', 'db');\n" + defines
    )
    _, credentials = site_descriptor_from_wp_config(tmp_path, config_file=config)
    with pytest.raises(CredentialsError) as caught:
        require_wp_config_credentials(credentials)
    assert caught.value.missing_key == missing
    assert caught.value.source == f"wp-config:{config}"
    assert "never-shown" not in str(caught.value)


def test_a_config_with_no_credentials_still_yields_a_descriptor(
    tmp_path: Path,
) -> None:
    """Credentials may come from another provider; the descriptor needs none."""
    config = _config(tmp_path, "define('DB_NAME', 'site');\ndefine('DB_HOST', 'db');\n")
    descriptor, credentials = site_descriptor_from_wp_config(
        tmp_path, config_file=config
    )
    assert descriptor.db_name == "site"
    archived, _ = site_descriptor_from_archive_config(
        tmp_path / "target", config_file=config
    )
    assert archived.db_name == "site"
    with pytest.raises(CredentialsError) as caught:
        require_wp_config_credentials(credentials)
    assert caught.value.missing_key is None


def test_a_full_credential_pair_is_required_without_error(tmp_path: Path) -> None:
    _, credentials = site_descriptor_from_wp_config(tmp_path, config_file=FIXTURE)
    require_wp_config_credentials(credentials)


def test_an_unreadable_config_names_the_path(tmp_path: Path) -> None:
    config = _config(tmp_path, "define('DB_NAME', 'site');\n")
    config.chmod(0)
    if os.access(config, os.R_OK):  # root: permission bits do not apply
        config.chmod(0o600)
        pytest.skip("permission bits are not enforced for this user")
    try:
        with pytest.raises(CredentialsError) as caught:
            site_descriptor_from_wp_config(tmp_path, config_file=config)
    finally:
        config.chmod(0o600)
    assert str(config) in caught.value.source


def test_a_missing_db_name_is_an_invalid_site(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        "define('DB_HOST', 'db');\ndefine('DB_USER', 'u');\n"
        "define('DB_PASSWORD', 'p');\n",
    )
    with pytest.raises(InvalidSiteDescriptorError, match="DB_NAME"):
        site_descriptor_from_wp_config(tmp_path, config_file=config)


def test_the_missing_extra_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "l3io.wp.database", None)
    with pytest.raises(MissingExtraError) as caught:
        site_descriptor_from_wp_config(tmp_path, config_file=FIXTURE)
    assert caught.value.extra == "database"


def test_a_planted_password_never_reaches_the_manifest(tmp_path: Path) -> None:
    """Every optional key set, the projection taken from the planted fixture."""
    descriptor, _ = site_descriptor_from_wp_config(tmp_path, config_file=FIXTURE)
    manifest = Manifest(
        format_version=FORMAT_VERSION,
        created_at="2026-10-27T09:15:00Z",
        backup_scope="all",
        source=source_from_descriptor(descriptor),
        encrypted=True,
        payload=ManifestPayload(ENCRYPTED_PAYLOAD_MEMBER, 1, "0" * 64),
        members=(
            ManifestMember(
                "config/wp-config.php", MemberRole.CONFIG, MemberKind.FILE, 1, None
            ),
        ),
        dump_present=False,
        origin=ManifestOrigin(OriginKind.SAFETY_SNAPSHOT, "overwrite", "run-1"),
        exclusions=ManifestExclusions(
            source="default",
            patterns=("*.log",),
            excluded=(ExcludedEntry("debug.log", "*.log", False),),
        ),
        config_path="wp-config.php",
    )
    text = manifest_to_json(manifest)
    assert descriptor.db_name in text  # the projection really is in there
    assert PLANTED_PASSWORD not in text
