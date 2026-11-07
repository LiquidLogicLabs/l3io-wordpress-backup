"""AD-13/AG-20 — how a preflight failure is decided, raised and rendered.

``found`` is ``absent``, the version found, ``unusable (<error>)``, or
``unusable (version unknown)`` when a minimum is stated and no version was
found (``DumpReadinessPreflight`` probes none, so it reports that); the
install hint renders as the last line and neither it nor the error is ever
folded into ``needed_for``. ``DumpReadinessPreflight`` maps ``check_dump``'s
verdict: unreachable and credential blockers raise what the real dump would,
any other blocker is an unusable tool naming its codes. The failure exits
``NOT_READY``. ``core/backup.py`` takes ``ToolRequirement`` from
``domain/preflight.py``, and the import-linter contracts carry no exemption
that would let ``core`` reach ``ports``.
"""

from __future__ import annotations

import ast
import dataclasses
import tomllib
from pathlib import Path

import pytest

from l3io.wp.backup.cli.exit import exit_code_for, render_failure
from l3io.wp.backup.cli.preflight import DumpReadinessPreflight, require_tools
from l3io.wp.backup.core import backup as core_backup
from l3io.wp.backup.core.backup import DUMP_REQUIREMENT
from l3io.wp.backup.domain.database import DatabaseLocation, Readiness
from l3io.wp.backup.domain.errors import (
    CredentialsError,
    DatabaseUnreachableError,
    PreflightFailedError,
)
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.domain.preflight import (
    ToolRequirement,
    ToolStatus,
    preflight_failure,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
LOCATION = DatabaseLocation(host="db.example", port=3306, name="wptest")
AGE = ToolRequirement(
    name="age",
    minimum_version=(1, 1),
    needed_for="passphrase encryption",
    install_hint="apt install age",
)


def _status(**overrides: object) -> ToolStatus:
    fields: dict[str, object] = {
        "name": "age",
        "present": True,
        "version": (1, 2),
        "path": "/usr/bin/age",
    }
    fields.update(overrides)
    return ToolStatus(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("status", "found"),
    [
        (None, "absent"),
        (_status(present=False, version=None, path=None), "absent"),
        (_status(version=(1, 0, 3)), "1.0.3"),
        (_status(error="exit 127"), "unusable (exit 127)"),
        (_status(version=None), "unusable (version unknown)"),
    ],
)
def test_found_names_what_was_found(status: ToolStatus | None, found: str) -> None:
    failure = preflight_failure(AGE, status)
    assert failure is not None
    assert failure.found == found
    assert failure.needed_for == "passphrase encryption"
    assert failure.install_hint == "apt install age"


@pytest.mark.parametrize("version", [(1, 1), (1, 2)])
def test_a_tool_new_enough_passes(version: tuple[int, ...]) -> None:
    assert preflight_failure(AGE, _status(version=version)) is None


def test_an_unprobed_tool_passes_only_without_a_minimum() -> None:
    bare = ToolRequirement("age", None, "passphrase encryption")
    assert preflight_failure(bare, _status(version=None)) is None
    assert preflight_failure(AGE, _status(version=None)) is not None


def test_the_install_line_is_last_and_never_inside_needed_for() -> None:
    failure = preflight_failure(AGE, _status(error="exit 127"))
    assert failure is not None
    rendered = render_failure(failure, redact=lambda t: t, with_traceback=False)
    lines = rendered.splitlines()
    assert lines[0].endswith(
        "age is needed for passphrase encryption; found: unusable (exit 127)"
    )
    assert lines[-1].startswith("install: apt install age")
    assert "install" not in failure.needed_for
    assert "exit 127" not in failure.needed_for


def test_no_hint_means_no_install_line() -> None:
    bare = ToolRequirement("tool", None, "a step")
    failure = preflight_failure(bare, None)
    assert failure is not None
    assert "install:" not in str(failure)
    assert str(failure) == "tool is needed for a step; found: absent"


def test_a_preflight_failure_exits_not_ready() -> None:
    assert exit_code_for(PreflightFailedError("t", "x", "absent")) is ExitCode.NOT_READY


class Query:
    """A ``DatabaseQuery`` double answering one verdict."""

    def __init__(self, readiness: Readiness) -> None:
        self.readiness = readiness
        self.asked: list[DatabaseLocation] = []

    def check_dump(self, location: DatabaseLocation) -> Readiness:
        self.asked.append(location)
        return self.readiness


def _preflight(*blockers: str) -> DumpReadinessPreflight:
    readiness = Readiness(ready=not blockers, blockers=blockers)
    return DumpReadinessPreflight(
        Query(readiness), LOCATION, credential_source="wp-config.php"
    )


def test_a_ready_dump_passes() -> None:
    require_tools(_preflight(), (DUMP_REQUIREMENT,))


def test_another_blocker_is_an_unusable_tool_naming_the_codes() -> None:
    with pytest.raises(PreflightFailedError) as caught:
        require_tools(
            _preflight("client_tool_missing", "client_server_skew"),
            (DUMP_REQUIREMENT,),
        )
    assert caught.value.found == (
        "unusable (check_dump: client_tool_missing, client_server_skew)"
    )
    assert caught.value.needed_for == DUMP_REQUIREMENT.needed_for
    assert "mysqldump" not in str(caught.value)  # ADR-0015


def test_an_unreachable_server_is_unreachable() -> None:
    with pytest.raises(DatabaseUnreachableError):
        require_tools(_preflight("unreachable"), (DUMP_REQUIREMENT,))


@pytest.mark.parametrize("code", ["authentication_failed", "credentials_unobtainable"])
def test_a_credential_blocker_is_a_credentials_error(code: str) -> None:
    with pytest.raises(CredentialsError) as caught:
        require_tools(_preflight(code), (DUMP_REQUIREMENT,))
    assert caught.value.source == "wp-config.php"


def test_no_requirement_asks_nothing() -> None:
    query = Query(Readiness(ready=False, blockers=("unreachable",)))
    preflight = DumpReadinessPreflight(query, LOCATION, credential_source="x")
    require_tools(preflight, ())
    assert query.asked == []


def test_another_tool_is_looked_up_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    preflight = _preflight()
    with pytest.raises(PreflightFailedError) as caught:
        require_tools(preflight, (AGE,))
    assert caught.value.found == "absent"
    tool = tmp_path / "age"
    tool.write_text("#!/bin/sh\n", encoding="utf-8")
    tool.chmod(0o755)
    # Found, but nothing probes its version: a stated minimum is not waived.
    with pytest.raises(PreflightFailedError) as caught:
        require_tools(preflight, (AGE,))
    assert caught.value.found == "unusable (version unknown)"
    require_tools(preflight, (dataclasses.replace(AGE, minimum_version=None),))


# --------------------------------------------------------------------------
# The layer: core names ToolRequirement through domain, never ports
# --------------------------------------------------------------------------


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }


def test_core_takes_tool_requirement_from_domain() -> None:
    modules = _imports(Path(core_backup.__file__))
    assert "l3io.wp.backup.domain.preflight" in modules
    assert not [m for m in modules if m.startswith("l3io.wp.backup.ports")]


def test_the_layer_contracts_carry_no_exemption() -> None:
    config = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text("utf-8"))
    contracts = config["tool"]["importlinter"]["contracts"]
    assert contracts, "no import-linter contract"
    pure = [
        c
        for c in contracts
        if "l3io.wp.backup.ports" in c.get("forbidden_modules", ())
        and "l3io.wp.backup.core" in c.get("source_modules", ())
    ]
    assert pure, "no contract forbids core -> ports"
    for contract in contracts:
        assert not contract.get("ignore_imports"), contract["name"]


def test_the_import_check_sees_a_ports_import(tmp_path: Path) -> None:
    planted = tmp_path / "x.py"
    planted.write_text("from l3io.wp.backup.ports.preflight import Preflight\n")
    assert "l3io.wp.backup.ports.preflight" in _imports(planted)


@pytest.mark.parametrize(
    ("status", "found"),
    [(_status(version=(1, 0, 3)), "1.0.3"), (_status(version=None), "unusable")],
)
def test_a_too_old_or_unknown_version_states_the_minimum(
    status: ToolStatus, found: str
) -> None:
    failure = preflight_failure(AGE, status)
    assert failure is not None
    assert failure.minimum == "1.1"
    assert f"found: {failure.found}; 1.1 or later is needed" in str(failure)
    assert found in str(failure)


def test_an_absent_tool_states_no_minimum() -> None:
    failure = preflight_failure(AGE, None)
    assert failure is not None
    assert failure.minimum is None
    assert "or later" not in str(failure)
