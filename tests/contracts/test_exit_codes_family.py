"""FR-21 — the family codes match ``l3io-wp-database``'s ``cli.Exit``.

``domain/`` may not import ``l3io.wp.database`` (AD-1), so conformance is
checked here. The scope is database's enum: a code it adds that this package
lacks fails, as does one this package numbers differently.
"""

from __future__ import annotations

import pytest

from l3io.wp.backup.domain.exit_codes import RESERVED, ExitCode

database_cli = pytest.importorskip("l3io.wp.database.cli")

FAMILY = list(database_cli.Exit)


def test_the_family_enum_is_not_empty() -> None:
    assert FAMILY, "l3io.wp.database.cli.Exit has no members: nothing was checked"
    assert len(FAMILY) >= 7


@pytest.mark.parametrize("member", FAMILY, ids=lambda m: m.name)
def test_every_family_code_is_ours_with_the_same_value(member: object) -> None:
    name = member.name  # type: ignore[attr-defined]
    assert name in ExitCode.__members__, f"family code {name} is missing"
    assert ExitCode[name] == member.value  # type: ignore[attr-defined]


def test_our_own_codes_avoid_the_family_and_its_reservation() -> None:
    family_values = {int(m.value) for m in FAMILY}
    own = [c for c in ExitCode if c.name not in database_cli.Exit.__members__]
    assert own, "no backup-only codes: the scope of this check is wrong"
    for code in own:
        assert int(code) not in family_values | RESERVED
    assert not RESERVED & family_values
