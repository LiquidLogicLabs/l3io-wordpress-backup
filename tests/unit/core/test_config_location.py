"""E007-S01-002 (FR-29, AD-11) — ``core/config_location.py``, pure placement.

``place_config`` is driven over every combination of the
:class:`~l3io.wp.backup.core.config_location.ConfigFacts` booleans (from
``dataclasses.fields``) x every Manifest value (``None``, each
``ConfigLocation`` value from the enum, and values outside it) x
``restores_files``. Nothing is typed out twice: the generator is a function
of the enum and the facts type, and a test plants an extra member and an
extra field in each to show the cases follow them.
"""

from __future__ import annotations

import dataclasses
import itertools
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath

import pytest

from l3io.wp.backup.core.config_location import (
    ConfigFacts,
    ConfigPlacement,
    config_file,
    existing_location,
    place_config,
)
from l3io.wp.backup.domain.archive_format import ConfigLocation
from l3io.wp.backup.domain.errors import (
    ConfigLocationConflictError,
    RefusedError,
    UnsupportedConfigLocationError,
)
from l3io.wp.backup.domain.exit_codes import ExitCode

ROOT = PurePosixPath("/srv/hardened/public")
#: Manifest values no Archive may name: each is refused, never joined.
FOREIGN = ("../../etc/x", "/abs/wp-config.php", "wp-config.php/../x", "")


def facts_combinations(facts_type: type) -> Iterator[dict[str, bool]]:
    """Every assignment of ``facts_type``'s boolean fields."""
    names = [f.name for f in dataclasses.fields(facts_type)]
    for values in itertools.product((False, True), repeat=len(names)):
        yield dict(zip(names, values, strict=True))


def manifest_values(enum: type[StrEnum]) -> tuple[str | None, ...]:
    return (None, *(member.value for member in enum), *FOREIGN)


CASES = [
    (manifest, ConfigFacts(**values), restores, has_config)
    for manifest in manifest_values(ConfigLocation)
    for values in facts_combinations(ConfigFacts)
    for restores in (False, True)
    for has_config in (False, True)
]


def _id(case: tuple[str | None, ConfigFacts, bool, bool]) -> str:
    manifest, facts, restores, has_config = case
    flags = "".join(
        "1" if getattr(facts, f.name) else "0" for f in dataclasses.fields(facts)
    )
    files = "files" if restores else "nofiles"
    return f"{manifest!r}-{flags}-{files}-{'config' if has_config else 'noconfig'}"


# --------------------------------------------------------------------------
# Scope: the cases follow the enum and the facts type
# --------------------------------------------------------------------------


def test_the_cases_cover_every_location_and_every_fact() -> None:
    assert {c[0] for c in CASES} >= {m.value for m in ConfigLocation} | {None}
    fields = [f.name for f in dataclasses.fields(ConfigFacts)]
    assert fields, "ConfigFacts has no field"
    assert len(CASES) == len(manifest_values(ConfigLocation)) * 2 ** len(fields) * 4


def test_a_planted_member_and_field_reach_the_cases() -> None:
    """Attacks the scope: a location or fact added later is generated, not missed."""
    planted = StrEnum(
        "Planted",
        {**{m.name: m.value for m in ConfigLocation}, "ELSEWHERE": "x/wp-config.php"},
    )

    @dataclass(frozen=True)
    class MoreFacts:
        at_site_root: bool
        at_parent: bool
        parent_has_wp_settings: bool
        at_planted_place: bool

    assert "x/wp-config.php" in manifest_values(planted)
    combos = list(facts_combinations(MoreFacts))
    assert len(combos) == 2**4
    assert any(c["at_planted_place"] for c in combos)


# --------------------------------------------------------------------------
# The rules, per case
# --------------------------------------------------------------------------


def _wanted(manifest: str | None) -> ConfigLocation | None:
    if manifest is None:
        return None
    return next((m for m in ConfigLocation if m.value == manifest), None)


@pytest.mark.parametrize("case", CASES, ids=_id)
def test_place_config(case: tuple[str | None, ConfigFacts, bool, bool]) -> None:
    manifest, facts, restores, has_config = case
    knobs = {
        "site_root": ROOT,
        "restores_files": restores,
        "archive_has_config": has_config,
    }
    if manifest is not None and _wanted(manifest) is None:
        with pytest.raises(UnsupportedConfigLocationError) as caught:
            place_config(manifest, facts, **knobs)
        assert caught.value.value == manifest
        assert manifest == "" or manifest in str(caught.value)
        return
    wanted = _wanted(manifest)
    existing = existing_location(facts)
    shadowed = (
        existing is ConfigLocation.SITE_ROOT
        and wanted is ConfigLocation.PARENT
        and facts.at_parent
        and not facts.parent_has_wp_settings
    )
    other_install = (
        existing is None
        and wanted is ConfigLocation.PARENT
        and facts.parent_has_wp_settings
    )
    if shadowed or other_install:
        with pytest.raises(ConfigLocationConflictError) as conflict:
            place_config(manifest, facts, **knobs)
        named = ROOT.parent / "wp-config.php" if shadowed else ROOT.parent
        assert conflict.value.path == named
        assert str(named) in str(conflict.value)
        return
    placed = place_config(manifest, facts, **knobs)
    if existing is not None:
        assert placed == ConfigPlacement(
            location=existing, action="use-existing", source="target"
        )
    elif wanted is None:
        # No recorded location (v0, or v1 with none): the default, no PARENT
        # invented, and the Archive's config laid there when a Mode restores
        # files (the ``make ci`` regression of 2026-11-07).
        lays = restores and has_config
        assert placed == ConfigPlacement(
            location=ConfigLocation.SITE_ROOT,
            action="lay-down" if lays else "none",
            source="v0-default",
        )
    else:
        assert placed.location is wanted
        assert placed.source == "manifest"
        # A Mode that restores no files never plans a lay-down (ADV-10).
        assert placed.action == ("lay-down" if restores else "none")


def test_existing_location_is_wp_load_php_s_rule() -> None:
    for values in facts_combinations(ConfigFacts):
        facts = ConfigFacts(**values)
        expected = (
            ConfigLocation.SITE_ROOT
            if facts.at_site_root
            else ConfigLocation.PARENT
            if facts.at_parent and not facts.parent_has_wp_settings
            else None
        )
        assert existing_location(facts) is expected, values


def test_the_refusals_are_refusals() -> None:
    for error in (UnsupportedConfigLocationError, ConfigLocationConflictError):
        assert issubclass(error, RefusedError)
        assert error.exit_code is ExitCode.REFUSED


# --------------------------------------------------------------------------
# config_file: the one anchoring, never a ``..``
# --------------------------------------------------------------------------


@pytest.mark.parametrize("location", list(ConfigLocation))
@pytest.mark.parametrize("root", [ROOT, PurePosixPath("/a/b/c/site")])
def test_config_file_anchors_to_the_root(
    location: ConfigLocation, root: PurePosixPath
) -> None:
    placement = ConfigPlacement(location=location, action="lay-down", source="manifest")
    path = config_file(root, placement)
    assert ".." not in path.parts
    assert path.name == "wp-config.php"
    assert path.is_absolute()
    expected_dir = root.parent if location is ConfigLocation.PARENT else root
    assert path.parent == expected_dir
