"""AD-3 — the Site Descriptor and the database values it yields."""

from __future__ import annotations

import dataclasses
from pathlib import PurePosixPath

import pytest

from l3io.wp.backup.domain.database import DatabaseLocation
from l3io.wp.backup.domain.errors import InvalidSiteDescriptorError
from l3io.wp.backup.domain.site import SiteDescriptor

FIELDS = {
    "site_root": PurePosixPath("/srv/www"),
    "db_host": "db",
    "db_port": 3307,
    "db_name": "wordpress",
    "table_prefix": "wp_",
    "site_url": None,
    "home_url": "https://example.test",
    "credential_source": "wp-config:/srv/www/wp-config.php",
}


def _descriptor(**changes: object) -> SiteDescriptor:
    return SiteDescriptor(**{**FIELDS, **changes})  # type: ignore[arg-type]


def test_the_descriptor_is_frozen_and_has_no_setter() -> None:
    descriptor = _descriptor()
    with pytest.raises(dataclasses.FrozenInstanceError):
        descriptor.db_name = "other"  # type: ignore[misc]
    assert dataclasses.replace(descriptor, db_name="other").db_name == "other"
    assert descriptor.db_name == "wordpress"


@pytest.mark.parametrize(
    "changes",
    [
        {"site_root": PurePosixPath("relative/root")},
        {"db_host": ""},
        {"db_name": ""},
        {"db_port": 0},
        {"db_port": 65536},
        {"db_port": True},
    ],
)
def test_invalid_values_are_refused_at_construction(changes: dict[str, object]) -> None:
    with pytest.raises(InvalidSiteDescriptorError):
        _descriptor(**changes)
    with pytest.raises(InvalidSiteDescriptorError):
        dataclasses.replace(_descriptor(), **changes)  # type: ignore[arg-type]


def test_repr_carries_no_credential() -> None:
    text = repr(_descriptor())
    assert "password" not in text.lower()
    assert "credential_source='wp-config:" in text
    names = {f.name for f in dataclasses.fields(SiteDescriptor)}
    assert not {"password", "username", "credentials", "db_password"} & names


def test_database_location() -> None:
    location = _descriptor().database_location()
    assert location == DatabaseLocation(host="db", port=3307, name="wordpress")
    renamed = location.with_name("wordpress_copy")
    assert renamed == DatabaseLocation(host="db", port=3307, name="wordpress_copy")
