"""AG-27 — the Storage Target grammar and the default Archive name."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import PurePosixPath
from urllib.parse import urlsplit

import pytest

from l3io.wp.backup.domain.errors import (
    RefusedError,
    UnsupportedStorageSchemeError,
    UsageError,
)
from l3io.wp.backup.domain.storage_target import (
    StorageTarget,
    default_archive_name,
    parse_storage_target,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/var/backups/site.tar.gz", "/var/backups/site.tar.gz"),
        ("relative/site.tar.gz", "relative/site.tar.gz"),
        ("file:///var/backups/site.tar.gz", "/var/backups/site.tar.gz"),
    ],
)
def test_a_file_target_parses(text: str, expected: str) -> None:
    parts = urlsplit(text)
    target = parse_storage_target(parts.scheme, parts.path)
    assert target == StorageTarget(scheme="file", path=PurePosixPath(expected))


def test_another_scheme_is_refused() -> None:
    parts = urlsplit("s3://bucket/site.tar.gz")
    with pytest.raises(UnsupportedStorageSchemeError) as caught:
        parse_storage_target(parts.scheme, parts.path)
    assert caught.value.scheme == "s3"
    assert isinstance(caught.value, RefusedError)


def test_the_default_name_is_utc() -> None:
    eastern = timezone(timedelta(hours=-5))
    now = datetime(2026, 10, 27, 4, 15, 0, tzinfo=eastern)
    assert default_archive_name("wordpress", now) == (
        "wpbackup2-wordpress-20261027091500.tar.gz"
    )
    utc_now = datetime(2026, 10, 27, 9, 15, 0, tzinfo=UTC)
    assert default_archive_name("wordpress", utc_now) == (
        "wpbackup2-wordpress-20261027091500.tar.gz"
    )


def test_a_naive_time_is_refused() -> None:
    with pytest.raises(UsageError):
        default_archive_name("wordpress", datetime(2026, 10, 27, 9, 15))
