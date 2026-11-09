"""BL-E004-036, owner decision 46 — a v0 view's ``config_path`` is a location.

A v0 Archive (no Manifest) holding ``wp-root/wp-config.php`` gets the
``config_path`` a v1 Archive with its config in the Site Root tree records,
``ConfigLocation.SITE_ROOT``, so every reader takes one meaning: the plan's
cutover check, ``config_member``, ``recorded_location`` and the
``--overwrite-wp-config`` check treat the two alike. Before the fix the v0
view named the member path, the cutover check never matched it, and an
``all`` restore onto a target with no ``wp-config.php`` was refused
``NoConfigForCutoverError``.
"""

from __future__ import annotations

import pytest

from l3io.wp.backup.core.config_location import recorded_location
from l3io.wp.backup.core.restore import ArchiveHasNoConfigError
from l3io.wp.backup.domain.archive_format import (
    V0_DUMP_MEMBER,
    V0_SITE_ROOT,
    ArchiveView,
    ConfigLocation,
    MemberIndex,
    MemberRole,
    classify,
    config_member,
    member_relative,
)
from l3io.wp.backup.domain.restore import (
    ConfigPolicy,
    RestoreMode,
)
from tests.restore_builders import EMPTY, POPULATED, plan, view

V0_CONFIG = f"{V0_SITE_ROOT}/wp-config.php"


def _v0(*, config: bool = True) -> ArchiveView:
    names = (V0_SITE_ROOT, f"{V0_SITE_ROOT}/index.php", V0_DUMP_MEMBER)
    if config:
        names = (*names, V0_CONFIG)
    return classify(
        MemberIndex(
            names=names,
            first_member=names[0],
            manifest_text=None,
            readable_to_end=True,
            gzip=True,
        )
    )


def test_a_v0_view_names_the_location_a_v1_in_tree_view_records() -> None:
    v0, v1 = _v0(), view()
    assert v0.config_path == ConfigLocation.SITE_ROOT.value
    assert v0.config_path == v1.config_path


def test_the_v0_location_resolves_to_the_trees_member() -> None:
    v0, v1 = _v0(), view()
    member = config_member(v0)
    assert member is not None
    assert member.path == V0_CONFIG
    assert member.role is MemberRole.SITE_ROOT
    tree = config_member(v1)
    assert tree is not None
    assert member_relative(v0, member.path) == member_relative(v1, tree.path)


def test_a_v0_view_without_a_config_names_none() -> None:
    v0 = _v0(config=False)
    assert v0.config_path is None
    assert config_member(v0) is None


def test_neither_records_a_location_without_a_config_role_member() -> None:
    assert recorded_location(_v0()) is None
    assert recorded_location(view()) is None


@pytest.mark.parametrize("mode", list(RestoreMode))
def test_onto_a_target_with_no_config_v0_plans_as_v1_does(mode: RestoreMode) -> None:
    """Decision 46: a v0 Archive plans as a v1 one, every Mode.

    Owner decision 49: without relocation no Mode cuts over, so none refuses
    for want of a config to write (``NoConfigForCutoverError`` is a
    relocation's only).
    """
    built = plan(mode, target=EMPTY, archive_view=_v0())
    assert built.phases == plan(mode, target=EMPTY, archive_view=view()).phases


def test_overwrite_accepts_a_v0_config_and_refuses_a_v0_without_one() -> None:
    plan(RestoreMode.ALL, archive_view=_v0(), config_policy=ConfigPolicy.OVERWRITE)
    with pytest.raises(ArchiveHasNoConfigError):
        plan(
            RestoreMode.ALL,
            target=POPULATED,
            archive_view=_v0(config=False),
            config_policy=ConfigPolicy.OVERWRITE,
        )
