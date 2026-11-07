"""FR-22 — a library caller type-checks under ``mypy --strict`` (A13).

CI's mypy step checks ``src`` only, so the consumer script
``tests/typing/api_consumer.py`` is checked here, through ``mypy.api.run``
with the project's own ``[tool.mypy]`` (``pyproject.toml``, from the project
root). It must pass with no output; a copy with one ill-typed call (a ``str``
as the Restore Mode) must fail, so a check that sees nothing cannot pass.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mypy import api as mypy_api

ROOT = Path(__file__).resolve().parents[2]
CONSUMER = ROOT / "tests" / "typing" / "api_consumer.py"
CONFIG = ROOT / "pyproject.toml"


def _mypy(path: Path) -> tuple[int, list[str]]:
    out, err, code = mypy_api.run(
        ["--strict", "--config-file", str(CONFIG), "--no-error-summary", str(path)]
    )
    return code, [line for line in (out + err).splitlines() if line.strip()]


@pytest.fixture(autouse=True)
def _at_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(ROOT)


def test_the_consumer_type_checks_clean() -> None:
    code, lines = _mypy(CONSUMER)
    assert (code, lines) == (0, [])


def test_an_ill_typed_call_fails(tmp_path: Path) -> None:
    text = CONSUMER.read_text(encoding="utf-8")
    assert "mode=RestoreMode.ALL," in text
    planted = tmp_path / "api_consumer_planted.py"
    planted.write_text(
        text.replace("mode=RestoreMode.ALL,", 'mode="all",'), encoding="utf-8"
    )
    code, lines = _mypy(planted)
    assert code == 1
    assert any('"mode"' in line and "RestoreMode" in line for line in lines), lines
