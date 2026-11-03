"""FR-21 — the exit-code table (``domain/exit_codes.py``).

Every check is parametrised from ``list(ExitCode)``: the scope is the enum,
never a hand-kept list, so a member added without a description, or on a
reserved value, fails here.
"""

from __future__ import annotations

import pytest

from l3io.wp.backup.domain.exit_codes import DESCRIPTIONS, RESERVED, ExitCode

MEMBERS = list(ExitCode)


def test_the_scope_is_the_enum() -> None:
    assert len(MEMBERS) == len(ExitCode.__members__)
    assert ExitCode.OK in MEMBERS


@pytest.mark.parametrize("code", MEMBERS, ids=lambda c: c.name)
def test_every_member_has_a_one_line_description(code: ExitCode) -> None:
    text = DESCRIPTIONS[code]
    assert text.strip() == text
    assert text
    assert "\n" not in text


@pytest.mark.parametrize("code", MEMBERS, ids=lambda c: c.name)
def test_no_member_takes_a_reserved_value(code: ExitCode) -> None:
    assert int(code) not in RESERVED


def test_descriptions_name_only_members() -> None:
    assert set(DESCRIPTIONS) == set(MEMBERS)


def test_values_are_unique_and_only_ok_is_zero() -> None:
    values = [int(code) for code in MEMBERS]
    assert len(values) == len(set(values))
    assert [code for code in MEMBERS if int(code) == 0] == [ExitCode.OK]


def test_six_is_reserved_and_not_a_member() -> None:
    assert 6 in RESERVED
    with pytest.raises(ValueError, match="6"):
        ExitCode(6)
