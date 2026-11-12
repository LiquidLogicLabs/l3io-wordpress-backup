"""Owner decision 83 — which Archives ``wpbackup2 prune --keep N`` removes (pure).

The same "this Site's Archives" as ``--archive latest``
(``core/archive_choice.py``): ordered newest first by Manifest ``created_at``,
a tie by the later file name; the newest ``keep`` are kept and the rest go.
Another site's Archive, a Safety Snapshot, an unreadable Manifest and an
unparsable ``created_at`` are never candidates, whatever their names.
"""

from __future__ import annotations

from pathlib import PurePosixPath

import pytest

from l3io.wp.backup.core.archive_choice import (
    LatestCandidate,
    choose_prune,
)
from l3io.wp.backup.domain.archive_format import ManifestOrigin, OriginKind
from l3io.wp.backup.domain.errors import UsageError
from tests.unit.core.test_archive_choice import DIRECTORY, SITE, _manifest


def _c(name: str, created: str | None, **source: object) -> LatestCandidate:
    manifest = None if created is None else _manifest(created, **source)
    return LatestCandidate(path=DIRECTORY / name, manifest=manifest)


def _names(paths: tuple[PurePosixPath, ...]) -> list[str]:
    return [p.name for p in paths]


MIXED = [
    _c("wpbackup2-wpdb-20261101030000.tar.gz", "2026-11-01T03:00:00Z"),
    _c("wpbak-wpdb-20261102T030000Z.tar.gz", "2026-11-02T03:00:00Z"),
    _c("renamed.tar.gz", "2026-11-03T03:00:00Z"),
    _c("wpbak-wpdb-20261104T030000Z.tar.gz", "2026-11-04T03:00:00Z"),
    # Another site's, oldest of all, and one newer than every one of ours.
    _c("wpbak-other-20261031T030000Z.tar.gz", "2026-10-31T03:00:00Z", db_name="other"),
    _c(
        "wpbak-wpdb-20261105T030000Z.tar.gz", "2026-11-05T03:00:00Z", site_root="/srv/b"
    ),
    # Unreadable, and an unparsable created_at: never candidates.
    _c("corrupt.tar.gz", None),
    _c("wpbak-wpdb-19990101T000000Z.tar.gz", "not a time"),
]


def test_the_newest_keep_are_kept_and_only_this_sites_older_ones_go() -> None:
    choice = choose_prune(MIXED, SITE, keep=2)
    assert _names(choice.kept) == [
        "wpbak-wpdb-20261104T030000Z.tar.gz",
        "renamed.tar.gz",
    ]
    assert _names(tuple(a.path for a in choice.pruned)) == [
        "wpbak-wpdb-20261102T030000Z.tar.gz",
        "wpbackup2-wpdb-20261101030000.tar.gz",
    ]
    assert choice.pruned[0].created_at == "2026-11-02T03:00:00Z"
    assert (choice.considered, choice.matched) == (8, 4)


def test_a_safety_snapshot_is_never_pruned() -> None:
    import dataclasses

    snapshot = _manifest("2026-10-01T00:00:00Z")
    snapshot = dataclasses.replace(
        snapshot,
        origin=ManifestOrigin(
            kind=OriginKind.SAFETY_SNAPSHOT, restore_mode="all", run_id="r"
        ),
    )
    candidates = [
        LatestCandidate(path=DIRECTORY / "snap.tar.gz", manifest=snapshot),
        _c("a.tar.gz", "2026-11-01T00:00:00Z"),
        _c("b.tar.gz", "2026-11-02T00:00:00Z"),
    ]
    choice = choose_prune(candidates, SITE, keep=1)
    assert _names(tuple(a.path for a in choice.pruned)) == ["a.tar.gz"]


def test_ties_keep_the_later_file_name_as_latest_does() -> None:
    same = "2026-11-02T00:00:00Z"
    choice = choose_prune([_c("a.tar.gz", same), _c("b.tar.gz", same)], SITE, keep=1)
    assert _names(choice.kept) == ["b.tar.gz"]
    assert _names(tuple(a.path for a in choice.pruned)) == ["a.tar.gz"]


def test_fewer_than_keep_prunes_nothing_and_no_archive_is_no_error() -> None:
    assert choose_prune(MIXED, SITE, keep=10).pruned == ()
    assert choose_prune([], SITE, keep=1).pruned == ()


@pytest.mark.parametrize("keep", [0, -1])
def test_keep_below_one_is_refused_so_the_newest_always_stays(keep: int) -> None:
    with pytest.raises(UsageError) as caught:
        choose_prune(MIXED, SITE, keep=keep)
    assert caught.value.arguments == ("keep",)
