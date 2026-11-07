"""FR-22 — a Site Descriptor with invalid values fails at construction, by name.

Through the re-exported class, which is Epic 2's by identity.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

import pytest

from l3io.wp.backup import api
from l3io.wp.backup.domain.errors import InvalidSiteDescriptorError
from l3io.wp.backup.domain.site import SiteDescriptor

VALID: dict[str, Any] = {
    "site_root": PurePosixPath("/srv/www"),
    "db_host": "db.example",
    "db_port": 3306,
    "db_name": "wordpress",
    "table_prefix": "wp_",
    "site_url": None,
    "home_url": None,
    "credential_source": "wp-config.php",
}


def test_the_export_is_epic_2s_class() -> None:
    assert api.SiteDescriptor is SiteDescriptor


def test_valid_values_construct() -> None:
    assert api.SiteDescriptor(**VALID).db_port == 3306


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("site_root", PurePosixPath("relative/www")),
        ("db_host", ""),
        ("db_name", ""),
        ("db_port", 0),
        ("db_port", 65536),
        ("db_port", True),
    ],
    ids=["relative-root", "empty-host", "empty-name", "port-0", "port-65536", "bool"],
)
def test_an_invalid_value_fails_at_construction(field: str, value: object) -> None:
    with pytest.raises(InvalidSiteDescriptorError):
        api.SiteDescriptor(**(VALID | {field: value}))
