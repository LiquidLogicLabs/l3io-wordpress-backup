"""FR-5/ADR-0017 — the Site Root traversal and the exclusion policy."""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import pytest
from structlog.testing import capture_logs

from l3io.wp.backup.adapters.tar import collect_site_root
from l3io.wp.backup.domain.archive_format import MemberKind, MemberRole, member_path
from l3io.wp.backup.domain.errors import ArchiveWriteError, BackupError
from l3io.wp.backup.domain.exclusion import (
    INCLUDE_EVERYTHING,
    ExcludedEntry,
    ExclusionDecision,
)

SITE_ROOT = member_path(MemberRole.SITE_ROOT, "")


@dataclass
class RecordingPolicy:
    """Excludes the paths in ``exclude`` and records every decision asked for."""

    exclude: frozenset[str] = frozenset()
    source: str = "custom"
    patterns: tuple[str, ...] = ("planted",)
    asked: list[tuple[str, bool]] = field(default_factory=list)

    def decide(self, relative: PurePosixPath, *, is_dir: bool) -> ExclusionDecision:
        self.asked.append((str(relative), is_dir))
        excluded = str(relative) in self.exclude
        return ExclusionDecision(
            excluded=excluded, pattern="planted" if excluded else None, protected=False
        )


def _names(site_root: Path, policy: object = INCLUDE_EVERYTHING) -> list[str]:
    collection = collect_site_root(site_root, policy)  # type: ignore[arg-type]
    return [m.path for m in collection.members]


def test_every_entry_is_listed_in_sorted_pre_order(site_root: Path) -> None:
    def site(relative: str) -> str:
        return member_path(MemberRole.SITE_ROOT, relative)

    assert _names(site_root) == [
        SITE_ROOT,
        site("index.php"),
        site("wp-config.php"),
        site("wp-content"),
        site("wp-content/empty"),
        site("wp-content/uploads"),
        site("wp-content/uploads/a.bin"),
    ]


def test_the_policy_sees_each_entry_once_and_never_a_pruned_child(
    site_root: Path,
) -> None:
    (site_root / "wp-content" / "cache" / "deep").mkdir(parents=True)
    (site_root / "wp-content" / "cache" / "deep" / "x").write_text("x")
    policy = RecordingPolicy(exclude=frozenset({"wp-content/cache"}))
    collect_site_root(site_root, policy)  # type: ignore[arg-type]
    asked = [path for path, _ in policy.asked]
    assert len(asked) == len(set(asked))
    assert "wp-content/cache" in asked
    assert not [p for p in asked if p.startswith("wp-content/cache/")]
    assert ("wp-content/uploads", True) in policy.asked
    assert ("index.php", False) in policy.asked


def test_only_the_top_most_pruned_path_is_recorded(site_root: Path) -> None:
    (site_root / "wp-content" / "cache" / "deep").mkdir(parents=True)
    policy = RecordingPolicy(
        exclude=frozenset({"wp-content/cache", "wp-content/cache/deep", "index.php"})
    )
    collection = collect_site_root(site_root, policy)  # type: ignore[arg-type]
    assert collection.excluded == (
        ExcludedEntry(path="index.php", pattern="planted", is_dir=False),
        ExcludedEntry(path="wp-content/cache", pattern="planted", is_dir=True),
    )
    names = [m.path for m in collection.members]
    assert not [n for n in names if "cache" in n]


def test_wp_config_at_the_site_root_is_never_excluded(site_root: Path) -> None:
    (site_root / "sub").mkdir()
    (site_root / "sub" / "wp-config.php").write_text("nested")
    policy = RecordingPolicy(exclude=frozenset({"wp-config.php", "sub/wp-config.php"}))
    collection = collect_site_root(site_root, policy)  # type: ignore[arg-type]
    names = [m.path for m in collection.members]
    assert member_path(MemberRole.SITE_ROOT, "wp-config.php") in names
    # Only the Site Root's own wp-config.php is protected.
    assert member_path(MemberRole.SITE_ROOT, "sub/wp-config.php") not in names
    assert [e.path for e in collection.excluded] == ["sub/wp-config.php"]


def test_a_raising_policy_fails_naming_the_path(site_root: Path) -> None:
    class Raising:
        source = "custom"
        patterns = ()

        def decide(self, relative: PurePosixPath, *, is_dir: bool) -> ExclusionDecision:
            if str(relative) == "index.php":
                msg = "policy broke"
                raise ValueError(msg)
            return ExclusionDecision(excluded=False, pattern=None, protected=False)

    with pytest.raises(ArchiveWriteError) as caught:
        collect_site_root(site_root, Raising())  # type: ignore[arg-type]
    assert "index.php" in str(caught.value)
    assert isinstance(caught.value, BackupError)


def test_symlinks_are_never_followed(site_root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("not in the archive")
    (site_root / "out").symlink_to(outside)
    (site_root / "rel").symlink_to("index.php")
    with capture_logs() as logs:
        collection = collect_site_root(site_root, INCLUDE_EVERYTHING)
    by_path = {m.path: m for m in collection.members}
    out = by_path[member_path(MemberRole.SITE_ROOT, "out")]
    assert out.kind is MemberKind.SYMLINK
    assert out.link_target == str(outside)
    assert not [p for p in by_path if p.endswith("secret.txt")]
    rel = by_path[member_path(MemberRole.SITE_ROOT, "rel")]
    assert (rel.kind, rel.link_target) == (MemberKind.SYMLINK, "index.php")
    warned = [e for e in logs if e["event"] == "symlink_outside_site_root"]
    assert [e["path"] for e in warned] == ["out"]


def test_a_relative_link_climbing_out_is_warned(site_root: Path) -> None:
    (site_root / "wp-content" / "up").symlink_to("../../..")
    (site_root / "wp-content" / "ok").symlink_to("../index.php")
    with capture_logs() as logs:
        collect_site_root(site_root, INCLUDE_EVERYTHING)
    warned = [e["path"] for e in logs if e["event"] == "symlink_outside_site_root"]
    assert warned == ["wp-content/up"]


def test_a_hard_linked_pair_is_two_files_logged_once(site_root: Path) -> None:
    os.link(site_root / "index.php", site_root / "twin.php")
    with capture_logs() as logs:
        collection = collect_site_root(site_root, INCLUDE_EVERYTHING)
    kinds = {m.path: m.kind for m in collection.members}
    assert kinds[member_path(MemberRole.SITE_ROOT, "index.php")] is MemberKind.FILE
    assert kinds[member_path(MemberRole.SITE_ROOT, "twin.php")] is MemberKind.FILE
    logged = [e for e in logs if e["event"] == "hard_link_stored_as_file"]
    assert len(logged) == 1
    assert sorted(logged[0]["paths"]) == ["index.php", "twin.php"]


def test_a_socket_is_skipped_and_logged(site_root: Path) -> None:
    path = site_root / "s.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(path))
    except OSError:  # pragma: no cover - a path too long for AF_UNIX
        pytest.skip("cannot bind a unix socket under tmp_path")
    try:
        with capture_logs() as logs:
            collection = collect_site_root(site_root, INCLUDE_EVERYTHING)
    finally:
        server.close()
    assert not [m for m in collection.members if m.path.endswith("s.sock")]
    skipped = [e["path"] for e in logs if e["event"] == "special_file_skipped"]
    assert skipped == ["s.sock"]


def test_a_fifo_is_skipped(site_root: Path) -> None:
    os.mkfifo(site_root / "pipe")
    collection = collect_site_root(site_root, INCLUDE_EVERYTHING)
    assert not [m for m in collection.members if m.path.endswith("pipe")]


def test_an_empty_site_root_is_one_dir_entry(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    collection = collect_site_root(root, INCLUDE_EVERYTHING)
    assert [(m.path, m.kind) for m in collection.members] == [
        (SITE_ROOT, MemberKind.DIR)
    ]


def test_a_directory_swapped_for_a_symlink_mid_walk_is_not_descended(
    site_root: Path, tmp_path: Path
) -> None:
    """Swapped after its entry is seen and before it is listed."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("root only")
    uploads = site_root / "wp-content" / "uploads"

    class Swapping(RecordingPolicy):
        def decide(self, relative: PurePosixPath, *, is_dir: bool) -> ExclusionDecision:
            if str(relative) == "wp-content/uploads":
                uploads.rename(tmp_path / "moved")
                uploads.symlink_to(outside)
            return super().decide(relative, is_dir=is_dir)

    with pytest.raises(ArchiveWriteError) as caught:
        collect_site_root(site_root, Swapping())  # type: ignore[arg-type]
    assert caught.value.path == uploads


def test_a_directory_replaced_by_another_mid_walk_is_refused(
    site_root: Path, tmp_path: Path
) -> None:
    uploads = site_root / "wp-content" / "uploads"

    class Replacing(RecordingPolicy):
        def decide(self, relative: PurePosixPath, *, is_dir: bool) -> ExclusionDecision:
            if str(relative) == "wp-content/uploads":
                uploads.rename(tmp_path / "moved")
                uploads.mkdir()
            return super().decide(relative, is_dir=is_dir)

    with pytest.raises(ArchiveWriteError) as caught:
        collect_site_root(site_root, Replacing())  # type: ignore[arg-type]
    assert "changed" in caught.value.reason
