"""Owner decision 64 — which Archive ``--archive latest`` names (pure).

The newest by ``created_at`` of this Site's own backups: the Site Root, the
database host (any case), port and name, and the table prefix must all match
what the Manifest records; a Safety Snapshot, an unreadable Manifest and an
unparsable ``created_at`` never count; a tie goes to the later file name.
"""

from __future__ import annotations

import dataclasses
from pathlib import PurePosixPath

import pytest

from l3io.wp.backup.core.archive_choice import (
    ArchiveSite,
    LatestCandidate,
    choose_latest,
)
from l3io.wp.backup.domain.archive_format import (
    FORMAT_VERSION,
    Manifest,
    ManifestOrigin,
    OriginKind,
    pending_payload,
)
from l3io.wp.backup.domain.errors import NoArchiveForSiteError
from l3io.wp.backup.domain.exit_codes import ExitCode

SITE = ArchiveSite(
    site_root="/var/www/html",
    db_host="db",
    db_port=3306,
    db_name="wpdb",
    table_prefix="wp_",
)
DIRECTORY = PurePosixPath("/backups")


def _manifest(created_at: str, **source: object) -> Manifest:
    values: dict[str, object] = {
        "site_root": SITE.site_root,
        "db_host": SITE.db_host,
        "db_port": SITE.db_port,
        "db_name": SITE.db_name,
        "table_prefix": SITE.table_prefix,
        "site_url": None,
        "home_url": None,
    }
    values.update(source)
    return Manifest(
        format_version=FORMAT_VERSION,
        created_at=created_at,
        backup_scope="all",
        source=values,  # type: ignore[arg-type]
        encrypted=False,
        payload=pending_payload(encrypted=False),
        members=(),
        dump_present=True,
    )


def _candidate(name: str, manifest: Manifest | None) -> LatestCandidate:
    return LatestCandidate(path=DIRECTORY / name, manifest=manifest)


def test_the_newest_of_this_sites_archives_is_chosen() -> None:
    choice = choose_latest(
        [
            _candidate("a.tar.gz", _manifest("2026-11-07T10:00:00Z")),
            # The earlier spelling (BL-E002-005) compares as a time, not text.
            _candidate("b.tar.gz", _manifest("2026-11-08T09:00:00.5+00:00")),
            _candidate("c.tar.gz", _manifest("2026-11-08T08:59:59Z")),
        ],
        SITE,
        directory=DIRECTORY,
    )
    assert choice.path == DIRECTORY / "b.tar.gz"
    assert choice.created_at == "2026-11-08T09:00:00.5+00:00"
    assert (choice.considered, choice.matched) == (3, 3)


@pytest.mark.parametrize(
    "other",
    [
        {"site_root": "/srv/other"},
        {"db_host": "db2"},
        {"db_port": 3307},
        {"db_name": "otherdb"},
        {"table_prefix": "wp2_"},
        {"db_port": None},
    ],
    ids=["root", "host", "port", "name", "prefix", "incomplete"],
)
def test_another_sites_newer_archive_is_never_chosen(other: dict[str, object]) -> None:
    choice = choose_latest(
        [
            _candidate("mine.tar.gz", _manifest("2026-11-07T10:00:00Z")),
            _candidate("theirs.tar.gz", _manifest("2026-11-09T10:00:00Z", **other)),
        ],
        SITE,
        directory=DIRECTORY,
    )
    assert choice.path.name == "mine.tar.gz"
    assert choice.matched == 1


def test_the_host_compares_in_any_case() -> None:
    choice = choose_latest(
        [_candidate("a.tar.gz", _manifest("2026-11-07T10:00:00Z", db_host="DB"))],
        SITE,
        directory=DIRECTORY,
    )
    assert choice.path.name == "a.tar.gz"


def test_snapshots_unreadable_and_undated_archives_never_count() -> None:
    snapshot = dataclasses.replace(
        _manifest("2026-11-10T10:00:00Z"),
        origin=ManifestOrigin(
            kind=OriginKind.SAFETY_SNAPSHOT, restore_mode="all", run_id="r"
        ),
    )
    choice = choose_latest(
        [
            _candidate("backup.tar.gz", _manifest("2026-11-07T10:00:00Z")),
            _candidate("snapshot.tar.gz", snapshot),
            _candidate("unreadable.tar.gz", None),
            _candidate("undated.tar.gz", _manifest("yesterday")),
        ],
        SITE,
        directory=DIRECTORY,
    )
    assert choice.path.name == "backup.tar.gz"
    assert (choice.considered, choice.matched) == (4, 1)


def test_a_tie_goes_to_the_later_name() -> None:
    when = "2026-11-07T10:00:00Z"
    choice = choose_latest(
        [
            _candidate("b.tar.gz", _manifest(when)),
            _candidate("a.tar.gz", _manifest(when)),
        ],
        SITE,
        directory=DIRECTORY,
    )
    assert choice.path.name == "b.tar.gz"


def test_none_for_this_site_is_a_usage_error_naming_the_site() -> None:
    with pytest.raises(NoArchiveForSiteError) as caught:
        choose_latest(
            [_candidate("x.tar.gz", _manifest("2026-11-07T10:00:00Z", db_name="x"))],
            SITE,
            directory=DIRECTORY,
        )
    error = caught.value
    assert error.exit_code is ExitCode.USAGE
    assert (error.considered, error.directory) == (1, DIRECTORY)
    assert "database wpdb on db:3306" in str(error)
    assert "/backups" in str(error)
