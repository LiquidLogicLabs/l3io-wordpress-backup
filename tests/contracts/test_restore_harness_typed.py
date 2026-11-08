"""CR-2 (BL-E003-122): the restore test harness is typed for substitution.

Every restore harness module (``tests/restore_*.py``, found by glob, asserted
non-empty) carries no ``type: ignore``: a port stand-in is ``typing.cast`` to
the port it fills, and a test that swaps the config halves or the
``load_database`` slot passes ``Harness(config_writer=..., slot=...)`` instead
of assigning over a typed attribute.

**Not covered:** the test modules that drive the harness (they are not
type-checked in CI; ``mypy --strict`` runs over ``src`` only), and a
suppression spelled other than ``type: ignore``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parents[1]
HARNESS = sorted(TESTS.glob("restore_*.py"))


def _suppressions(path: Path) -> list[str]:
    return [
        f"{path.name}:{number}"
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "type: ignore" in line
    ]


def test_the_harness_scope_is_found() -> None:
    assert {p.name for p in HARNESS} >= {"restore_harness.py", "restore_root_rig.py"}


@pytest.mark.parametrize("path", HARNESS, ids=[p.name for p in HARNESS])
def test_no_harness_module_suppresses_a_type_error(path: Path) -> None:
    assert _suppressions(path) == []


def test_a_planted_suppression_is_found(tmp_path: Path) -> None:
    planted = tmp_path / "restore_planted.py"
    planted.write_text("x: int = 'a'  # type: ignore[assignment]\n", encoding="utf-8")
    assert _suppressions(planted) == ["restore_planted.py:1"]
