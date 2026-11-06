"""BL-E002-116 — the size, free-space and expansion limits (E003-S01-001)."""

from __future__ import annotations

import pytest

from l3io.wp.backup.core.restore import (
    FREE_SPACE_MARGIN,
    MAX_EXPANSION_RATIO,
    SNAPSHOT_FILESYSTEM,
    TARGET_FILESYSTEM,
    WORKSPACE_FILESYSTEM,
    ExpansionRatioError,
    InsufficientSpaceError,
    SpaceClaim,
    check_claims,
    check_expansion,
    space_need,
)
from l3io.wp.backup.domain.archive_format import MemberRole
from l3io.wp.backup.domain.errors import ExitCode

FREE = 1_000_000
USABLE = int(FREE * (1 - FREE_SPACE_MARGIN))
OK = {
    "target_need": 0,
    "workspace_need": 0,
    "expanded": 100,
    "compressed": 100,
    "target_free": FREE,
    "workspace_free": FREE,
}


def test_the_limits_are_the_recorded_decision() -> None:
    assert FREE_SPACE_MARGIN == 0.05
    assert MAX_EXPANSION_RATIO == 200


@pytest.mark.parametrize(
    ("need", "free", "name"),
    [
        ("target_need", "target_free", TARGET_FILESYSTEM),
        ("workspace_need", "workspace_free", WORKSPACE_FILESYSTEM),
    ],
)
def test_free_space_at_and_one_byte_over_the_limit(
    need: str, free: str, name: str
) -> None:
    check_expansion(**{**OK, need: USABLE})
    with pytest.raises(InsufficientSpaceError) as caught:
        check_expansion(**{**OK, need: USABLE + 1})
    assert caught.value.exit_code is ExitCode.REFUSED
    assert caught.value.filesystem == name
    assert name in str(caught.value)
    assert str(USABLE + 1) in str(caught.value)
    del free


def test_the_ratio_at_and_one_over_the_limit() -> None:
    check_expansion(**{**OK, "expanded": MAX_EXPANSION_RATIO * 100})
    with pytest.raises(ExpansionRatioError) as caught:
        check_expansion(**{**OK, "expanded": (MAX_EXPANSION_RATIO + 1) * 100})
    assert caught.value.exit_code is ExitCode.REFUSED


def test_a_zero_byte_archive_that_expands_is_refused() -> None:
    with pytest.raises(ExpansionRatioError):
        check_expansion(**{**OK, "expanded": 1, "compressed": 0})


SIZES = [(MemberRole.SITE_ROOT, 100), (MemberRole.CONFIG, 10), (MemberRole.DUMP, 1000)]


def test_the_need_is_per_destination() -> None:
    need = space_need(
        SIZES, files=True, config=True, dump=True, config_copy=7, payload=500
    )
    assert (need.target, need.workspace, need.expanded) == (110, 1507, 1110)
    bare = space_need(
        SIZES, files=False, config=False, dump=False, config_copy=0, payload=0
    )
    assert (bare.target, bare.workspace, bare.expanded) == (0, 0, 1110)


def test_the_dump_never_lands_on_the_target() -> None:
    need = space_need(
        SIZES, files=True, config=False, dump=True, config_copy=0, payload=0
    )
    assert need.target == 100
    assert need.workspace == 1000


# --------------------------------------------------------------------------
# ADV-6 — claims summed per filesystem (E003-S02-003)
# --------------------------------------------------------------------------


def _claims(*devices: int, need: int) -> list[SpaceClaim]:
    names = (TARGET_FILESYSTEM, WORKSPACE_FILESYSTEM, SNAPSHOT_FILESYSTEM)
    return [
        SpaceClaim(name, device, need, FREE)
        for name, device in zip(names, devices, strict=True)
    ]


def test_claims_on_one_filesystem_are_summed() -> None:
    half = USABLE // 2 + 1
    claims = _claims(1, 1, 1, need=0)
    claims[0] = SpaceClaim(TARGET_FILESYSTEM, 1, half, FREE)
    claims[2] = SpaceClaim(SNAPSHOT_FILESYSTEM, 1, half, FREE)
    with pytest.raises(InsufficientSpaceError) as caught:
        check_claims(claims)
    assert caught.value.exit_code is ExitCode.REFUSED
    assert caught.value.needed == 2 * half
    assert caught.value.filesystem == f"{TARGET_FILESYSTEM} and {SNAPSHOT_FILESYSTEM}"


def test_claims_on_different_filesystems_are_not_summed() -> None:
    half = USABLE // 2 + 1
    check_claims(
        [
            SpaceClaim(TARGET_FILESYSTEM, 1, half, FREE),
            SpaceClaim(SNAPSHOT_FILESYSTEM, 2, half, FREE),
        ]
    )


def test_a_claim_that_fits_exactly_passes() -> None:
    check_claims([SpaceClaim(SNAPSHOT_FILESYSTEM, 1, USABLE, FREE)])
    with pytest.raises(InsufficientSpaceError, match="Safety Snapshot"):
        check_claims([SpaceClaim(SNAPSHOT_FILESYSTEM, 1, USABLE + 1, FREE)])


# --------------------------------------------------------------------------
# UX-4 (BL-E003-058): a refusal the operator can act on
# --------------------------------------------------------------------------

GIB = 1024**3


def test_a_space_refusal_names_the_path_readable_sizes_and_a_remedy() -> None:
    with pytest.raises(InsufficientSpaceError) as caught:
        check_expansion(
            **{**OK, "target_need": GIB, "target_free": GIB // 2},
            target_path="/srv/www/example",
        )
    text = str(caught.value)
    assert "'/srv/www/example'" in text
    assert "1024.0 MiB" in text
    assert "512.0 MiB" in text
    assert "free " in text
    assert "another filesystem" in text
    assert "nothing was changed" in text
    assert caught.value.paths == ("/srv/www/example",)
    assert (caught.value.needed, caught.value.free) == (GIB, GIB // 2)


def test_summed_claims_name_every_short_destination() -> None:
    half = USABLE // 2 + 1
    claims = [
        SpaceClaim(TARGET_FILESYSTEM, 1, half, FREE, "/srv/www/example"),
        SpaceClaim(WORKSPACE_FILESYSTEM, 1, 0, FREE, "/var/work/run"),
        SpaceClaim(SNAPSHOT_FILESYSTEM, 1, half, FREE, "/var/snapshots"),
    ]
    with pytest.raises(InsufficientSpaceError) as caught:
        check_claims(claims)
    assert caught.value.paths == ("/srv/www/example", "/var/snapshots")
    assert "'/var/snapshots'" in str(caught.value)


def test_the_expansion_refusal_says_there_is_no_override() -> None:
    with pytest.raises(ExpansionRatioError) as caught:
        check_expansion(**{**OK, "expanded": (MAX_EXPANSION_RATIO + 1) * 100})
    text = str(caught.value)
    assert "no override" in text
    assert "nothing was changed" in text
