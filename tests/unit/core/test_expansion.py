"""BL-E002-116 — the size, free-space and expansion limits (E003-S01-001)."""

from __future__ import annotations

import pytest

from l3io.wp.backup.core.restore import (
    FREE_SPACE_MARGIN,
    MAX_EXPANSION_RATIO,
    TARGET_FILESYSTEM,
    WORKSPACE_FILESYSTEM,
    ExpansionRatioError,
    InsufficientSpaceError,
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
