"""AG-27 — the Storage Target grammar and the default Archive name (decision 82)."""

from __future__ import annotations

import fnmatch
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
    DEFAULT_ARCHIVE_NAME_GLOBS,
    StorageTarget,
    archive_site_name,
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


def test_the_default_name_is_wpbak_site_and_a_sortable_utc_stamp() -> None:
    """Owner decision 82: ``wpbak-<site>-<YYYYMMDDTHHMMSSZ>.tar.gz``."""
    eastern = timezone(timedelta(hours=-5))
    now = datetime(2026, 10, 27, 4, 15, 0, tzinfo=eastern)
    assert default_archive_name("wordpress", now) == (
        "wpbak-wordpress-20261027T091500Z.tar.gz"
    )
    utc_now = datetime(2026, 10, 27, 9, 15, 0, tzinfo=UTC)
    assert default_archive_name("wordpress", utc_now) == (
        "wpbak-wordpress-20261027T091500Z.tar.gz"
    )


def test_names_sort_by_time() -> None:
    times = [
        datetime(2026, m, d, h, tzinfo=UTC)
        for m, d, h in ((9, 30, 23), (10, 1, 0), (10, 1, 9))
    ]
    names = [default_archive_name("site", t) for t in times]
    assert sorted(names) == names


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("example.com", "example.com"),
        ("My_Site-2", "My_Site-2"),
        ("my site", "my-site"),
        ("a/b\\c", "a-b-c"),
        ("  café & bar!  ", "caf-bar"),
        ("../etc/passwd", "..-etc-passwd"),
        ("-x-", "x"),
    ],
)
def test_the_site_part_keeps_only_safe_characters(given: str, expected: str) -> None:
    assert archive_site_name(given) == expected
    name = default_archive_name(given, datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC))
    assert name == f"wpbak-{expected}-20260102T030405Z.tar.gz"
    assert "/" not in name


@pytest.mark.parametrize("given", ["", "   ", "!!!", "---", "/"])
def test_a_site_part_empty_after_sanitising_is_a_usage_error(given: str) -> None:
    with pytest.raises(UsageError) as caught:
        archive_site_name(given)
    assert caught.value.arguments == ("site_name",)
    assert "--site-name" in str(caught.value)
    with pytest.raises(UsageError):
        default_archive_name(given, datetime(2026, 1, 2, tzinfo=UTC))


def test_the_globs_cover_the_new_and_the_legacy_default_names() -> None:
    """Exclusion and the stale ``.partial`` sweep read these globs."""
    name = default_archive_name("db", datetime(2026, 11, 7, tzinfo=UTC))
    legacy = "wpbackup2-db-20261103184135.tar.gz"
    for each in (name, legacy):
        assert any(fnmatch.fnmatchcase(each, g) for g in DEFAULT_ARCHIVE_NAME_GLOBS)
    assert not any(
        fnmatch.fnmatchcase("site.tar.gz", g) for g in DEFAULT_ARCHIVE_NAME_GLOBS
    )


def test_a_naive_time_is_refused() -> None:
    with pytest.raises(UsageError):
        default_archive_name("wordpress", datetime(2026, 10, 27, 9, 15))
