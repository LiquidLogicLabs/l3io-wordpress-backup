"""FR-21 from the library side — every exported error names a failing ExitCode.

Scope is ``api.__all__``'s exception classes, so a class exported later is
checked without an edit here; E002-S01-005's ``test_exit_mapping.py`` holds
the CLI side.
"""

from __future__ import annotations

import pytest

import l3io.wp.backup.api as api
from l3io.wp.backup.domain.errors import BackupError, exit_code_of
from l3io.wp.backup.domain.exit_codes import ExitCode
from tests.exception_scope import exported_exceptions

EXPORTED = exported_exceptions(api.__all__, api)


def _bad(classes: dict[str, type]) -> list[str]:
    return sorted(
        name
        for name, cls in classes.items()
        if not isinstance(getattr(cls, "exit_code", None), ExitCode)
        or cls.exit_code is ExitCode.OK
    )


def test_the_scope_is_the_exported_errors() -> None:
    assert len(EXPORTED) > 50
    assert EXPORTED["BackupError"] is BackupError
    assert "UnexpectedError" in EXPORTED


@pytest.mark.parametrize("name", sorted(EXPORTED))
def test_every_exported_class_has_a_failing_exit_code(name: str) -> None:
    assert _bad({name: EXPORTED[name]}) == []


def test_unexpected_error_exits_failed_like_any_defect() -> None:
    assert api.UnexpectedError.exit_code is ExitCode.FAILED
    assert exit_code_of(ValueError()) is ExitCode.FAILED


def test_a_planted_class_with_ok_or_no_code_is_reported() -> None:
    class Succeeds(BackupError):
        exit_code = ExitCode.OK

    class NotAnExitCode(Exception):
        exit_code = 0

    assert _bad({"Succeeds": Succeeds, "NotAnExitCode": NotAnExitCode}) == [
        "NotAnExitCode",
        "Succeeds",
    ]
