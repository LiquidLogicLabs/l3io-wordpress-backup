"""BL-E002-116, ADV-6 — a restore's size, free-space and expansion rules.

Split from ``core/restore.py`` (BL-E003-109, SD-01-3's seam). :func:`space_need`
sums what each destination receives; :func:`check_expansion` refuses a
decompression bomb (:data:`MAX_EXPANSION_RATIO`) and a destination short of
space less :data:`FREE_SPACE_MARGIN`; :func:`check_claims` sums the
destinations that share one filesystem (ADV-6). **Advisory** when their sizes
come from an Archive not yet verified (ADV-12, SD-01-4): verification and
extraction's indexed-size cap enforce.

Pure: no filesystem is read here (``tools/check_purity.py``); the caller
measures free space and passes it in.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, assert_never

from l3io.wp.backup.domain.archive_format import (
    MemberRole,
)
from l3io.wp.backup.domain.errors import RefusedError

__all__ = [
    "FREE_SPACE_MARGIN",
    "MAX_EXPANSION_RATIO",
    "SNAPSHOT_FILESYSTEM",
    "TARGET_FILESYSTEM",
    "WORKSPACE_FILESYSTEM",
    "ExpansionRatioError",
    "InsufficientSpaceError",
    "SpaceClaim",
    "SpaceNeed",
    "check_claims",
    "check_expansion",
    "space_need",
]

#: BL-E002-116: the share of a filesystem's free space a restore leaves unused.
FREE_SPACE_MARGIN: Final = 0.05
#: BL-E002-116: the largest expanded-to-compressed ratio a restore accepts.
MAX_EXPANSION_RATIO: Final = 200
#: How :class:`InsufficientSpaceError` names the filesystem that is short.
TARGET_FILESYSTEM: Final = "restore target (Site Root filesystem)"
WORKSPACE_FILESYSTEM: Final = "Run workspace filesystem"
SNAPSHOT_FILESYSTEM: Final = "Safety Snapshot directory filesystem"


_MIB: Final = 1024 * 1024


def _size(count: int) -> str:
    """``count`` bytes for an operator: MiB to one decimal, then the exact bytes.

    A unit conversion only: the structured fields keep the raw integers.
    """
    return f"{count / _MIB:.1f} MiB ({count} bytes)"


class InsufficientSpaceError(RefusedError):
    """A destination filesystem cannot hold what the restore writes there.

    ``paths`` are the destinations on it, so the operator knows which mount
    to free (UX-4); the sizes stay raw bytes in ``needed`` and ``free``.
    """

    def __init__(
        self, filesystem: str, *, needed: int, free: int, paths: tuple[str, ...] = ()
    ) -> None:
        self.filesystem = filesystem
        self.needed = needed
        self.free = free
        self.paths = paths
        usable = int(free * (1 - FREE_SPACE_MARGIN))
        where = f" holding {', '.join(repr(p) for p in paths)}" if paths else ""
        super().__init__(
            f"the {filesystem}{where} needs {_size(needed)} and has "
            f"{_size(free)} free ({_size(usable)} usable after the "
            f"{FREE_SPACE_MARGIN:.0%} margin); free {_size(needed - usable)} "
            "there, or use a location on another filesystem (the Run workspace "
            "follows TMPDIR, the Safety Snapshot --snapshot-dir); nothing was "
            "changed"
        )


class ExpansionRatioError(RefusedError):
    """The Archive expands by more than :data:`MAX_EXPANSION_RATIO` times."""

    def __init__(self, *, expanded: int, compressed: int) -> None:
        self.expanded = expanded
        self.compressed = compressed
        super().__init__(
            f"the archive expands from {_size(compressed)} to {_size(expanded)}, "
            f"over the {MAX_EXPANSION_RATIO}x expansion limit, which has no "
            "override: the archive may be damaged or built to fill the disk; "
            "nothing was changed"
        )


# --------------------------------------------------------------------------
# BL-E002-116 — size, free space and expansion
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SpaceNeed:
    """What each destination must hold, and the Archive's expansion."""

    target: int
    workspace: int
    expanded: int


def space_need(
    sizes: Iterable[tuple[MemberRole, int]],
    *,
    files: bool,
    config: bool,
    dump: bool,
    config_copy: int,
    payload: int,
) -> SpaceNeed:
    """Sum member ``sizes`` per destination (E003 pre-build re-check 2).

    The target holds the ``site-root`` members when ``files`` and the
    ``config`` members when ``config`` (a lay-down); never the Dump. The
    workspace holds the decrypted ``payload`` (``0`` for a plain Archive),
    the Dump when ``dump``, and the ``config_copy`` bytes.
    """
    target = workspace = expanded = 0
    for role, size in sizes:
        expanded += size
        match role:
            case MemberRole.SITE_ROOT:
                target += size if files else 0
            case MemberRole.CONFIG:
                target += size if config else 0
            case MemberRole.DUMP:
                workspace += size if dump else 0
            case _:
                assert_never(role)
    return SpaceNeed(
        target=target, workspace=workspace + payload + config_copy, expanded=expanded
    )


def check_expansion(
    *,
    target_need: int,
    workspace_need: int,
    expanded: int,
    compressed: int,
    target_free: int,
    workspace_free: int,
    target_path: str | None = None,
    workspace_path: str | None = None,
) -> None:
    """Refuse a restore its destinations cannot hold, or a decompression bomb.

    :class:`ExpansionRatioError` when ``expanded / compressed`` exceeds
    :data:`MAX_EXPANSION_RATIO`; :class:`InsufficientSpaceError` when a need
    exceeds its filesystem's free space less :data:`FREE_SPACE_MARGIN`,
    naming the destination's path when given.

    Advisory when its sizes come from an Archive not yet verified (the
    restore's ``PREFLIGHT``, ADV-12): the check is only as true as the index
    it is given; verification and extraction's size cap enforce.
    """
    if expanded > MAX_EXPANSION_RATIO * compressed:
        raise ExpansionRatioError(expanded=expanded, compressed=compressed)
    usable = 1 - FREE_SPACE_MARGIN
    if target_need > target_free * usable:
        raise InsufficientSpaceError(
            TARGET_FILESYSTEM,
            needed=target_need,
            free=target_free,
            paths=() if target_path is None else (target_path,),
        )
    if workspace_need > workspace_free * usable:
        raise InsufficientSpaceError(
            WORKSPACE_FILESYSTEM,
            needed=workspace_need,
            free=workspace_free,
            paths=() if workspace_path is None else (workspace_path,),
        )


@dataclass(frozen=True, slots=True)
class SpaceClaim:
    """What one destination must hold: its filesystem's name, device and free bytes.

    ``path`` is the destination itself, named in a refusal (UX-4).
    """

    filesystem: str
    device: int
    need: int
    free: int
    path: str | None = None


def check_claims(claims: Iterable[SpaceClaim]) -> None:
    """ADV-6: refuse when the claims on one filesystem exceed its free space.

    Claims are grouped by ``device`` (``st_dev``) and summed, so a Safety
    Snapshot directory on the Site Root's filesystem is charged against the
    same free space as the extraction. :class:`InsufficientSpaceError` names
    every destination on the short filesystem; the margin is
    :data:`FREE_SPACE_MARGIN`, as in :func:`check_expansion`.
    """
    grouped: dict[int, list[SpaceClaim]] = {}
    for claim in claims:
        grouped.setdefault(claim.device, []).append(claim)
    usable = 1 - FREE_SPACE_MARGIN
    for members in grouped.values():
        need = sum(c.need for c in members)
        free = min(c.free for c in members)
        if need > free * usable:
            short = [c for c in members if c.need > 0] or members
            names = " and ".join(dict.fromkeys(c.filesystem for c in short))
            paths = tuple(dict.fromkeys(c.path for c in short if c.path is not None))
            raise InsufficientSpaceError(names, needed=need, free=free, paths=paths)
