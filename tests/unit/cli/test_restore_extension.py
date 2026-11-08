"""E003-S02-006 — Epic 7's seam: ``drive_restore``'s ``extension`` parameter.

Its default is the shipped factory
(``adapters/restore_extension.py::restore_extension``, BL-E003-099); there
is no build without one, so no "relocation unavailable" fallback
(BL-E003-094, BL-E007-011). No flags plans ``NO_RELOCATION``; a factory
passed in is used for one assertion per seam it fills.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from pathlib import Path

import pytest

pytest.importorskip("l3io.wp.database")

from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.adapters.restore_extension import restore_extension
from l3io.wp.backup.cli import restore as restore_mod
from l3io.wp.backup.cli.restore_request import RelocationFlags
from l3io.wp.backup.core.restore import NO_RELOCATION, RestoreRelocation
from l3io.wp.backup.domain.credentials import ExplicitCredentialSpec
from l3io.wp.backup.domain.preflight import ToolRequirement
from l3io.wp.backup.domain.restore import RestoreMode
from tests.conftest import CredentialSentinel
from tests.restore_root_rig import Rig

_TOOL = ToolRequirement("wp-e003-fake", None, "the fake extension")


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    return Rig(tmp_path, monkeypatch)


def _flags(**set_: object) -> RelocationFlags:
    return dataclasses.replace(RelocationFlags.none(), **set_)  # type: ignore[arg-type]


def test_the_shipped_extension_is_drive_restores_default() -> None:
    """BL-E003-099: a parameter, not a module global; no fallback is built in."""
    default = inspect.signature(restore_mod.drive_restore).parameters["extension"]
    assert default.default is restore_extension
    assert not hasattr(restore_mod, "RESTORE_EXTENSION")
    assert not hasattr(restore_mod, "_NoExtension")


def test_without_flags_the_plan_has_no_relocation(rig: Rig) -> None:
    rig.drive()
    assert rig.plan_calls[-1]["relocation"] == NO_RELOCATION


class _SearchReplace:
    def relocate_content(self, site_root: Path, urls: object) -> object:
        raise AssertionError


class _Extension:
    def __init__(self) -> None:
        self.relocation = dataclasses.replace(
            RestoreRelocation.none(), required_tools=()
        )
        self.replacer = _SearchReplace()
        self.spec = ExplicitCredentialSpec("relocated_user", "relocated-pw-e003-x")
        self.resolved: list[dict[str, object]] = []

    def required_tools(self) -> tuple[ToolRequirement, ...]:
        return (_TOOL,)

    def wordpress_spec(self) -> ExplicitCredentialSpec:
        return self.spec

    def search_replace(self) -> _SearchReplace:
        return self.replacer

    def resolve(self, **kwargs: object) -> RestoreRelocation:
        self.resolved.append(kwargs)
        return self.relocation


@pytest.fixture
def extension(rig: Rig) -> _Extension:
    ext = _Extension()
    calls: list[RelocationFlags] = []

    def factory(flags: RelocationFlags, *, secrets: SecretRegistry) -> _Extension:
        calls.append(flags)
        return ext

    rig.extension = factory  # type: ignore[assignment]
    return ext


def test_its_required_tools_reach_preflight_through_the_plan(
    rig: Rig, extension: _Extension
) -> None:
    rig.drive()
    assert _TOOL in rig.plans[-1].required_tools


def test_its_search_replace_reaches_real_adapters(
    rig: Rig, extension: _Extension
) -> None:
    rig.drive()
    assert rig.adapter_calls[-1]["search_replace"] is extension.replacer


def test_its_wordpress_spec_becomes_the_relocated_source(
    rig: Rig, extension: _Extension, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[tuple[object, object]] = []
    real = restore_mod.wordpress_source

    def recording(spec: object, **kwargs: object) -> object:
        source = real(spec, **kwargs)  # type: ignore[arg-type]
        built.append((spec, source))
        return source

    monkeypatch.setattr(restore_mod, "wordpress_source", recording)
    rig.drive()
    relocated = [source for spec, source in built if spec is extension.spec]
    assert len(relocated) == 1
    assert rig.adapter_calls[-1]["restore_wordpress_source"] is relocated[0]
    cutover = rig.executions[-1]["ports"].cutover_credentials
    assert cutover is not None
    assert cutover() == extension.spec.secret.reveal()


def test_its_relocation_reaches_build_restore_plan(
    rig: Rig, extension: _Extension
) -> None:
    rig.drive(mode=RestoreMode.FILES)
    assert rig.plan_calls[-1]["relocation"] is extension.relocation
    (call,) = extension.resolved
    assert call["mode"] is RestoreMode.FILES


# --------------------------------------------------------------------------
# The grant stays one line; the password stays out of every surface
# --------------------------------------------------------------------------


def _wpcli_lines(source: str) -> list[str]:
    """Each statement naming Epic 7's modules, unless it is the grant line.

    Epic 7 is ``adapters/wpcli*.py`` and ``adapters/restore_extension.py``
    (BL-E007-019 split one module into those).
    """
    offenders = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.stmt) or isinstance(
            node, ast.FunctionDef | ast.ClassDef | ast.Module
        ):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue  # a docstring is prose, not a reference
        text = ast.unparse(node)
        named = "wpcli" in text or "restore_extension" in text
        if not named or isinstance(node, ast.If | ast.With | ast.Try):
            continue
        grant = (
            isinstance(node, ast.ImportFrom)
            and node.module == "l3io.wp.backup.adapters.restore_extension"
            and [(a.name, a.asname) for a in node.names]
            == [("restore_extension", None)]
        )
        if not grant:
            offenders.append(text)
    return offenders


def test_only_the_extension_line_may_name_adapters_wpcli() -> None:
    source = Path(inspect.getfile(restore_mod)).read_text(encoding="utf-8")
    assert _wpcli_lines(source) == []
    grant = "from l3io.wp.backup.adapters.restore_extension import restore_extension"
    assert [line for line in source.splitlines() if line == grant] != []


def test_a_second_wpcli_reference_is_caught() -> None:
    grant = "from l3io.wp.backup.adapters.restore_extension import restore_extension\n"
    assert _wpcli_lines(grant) == []
    planted = grant + "from l3io.wp.backup.adapters import wpcli\n"
    assert len(_wpcli_lines(planted)) == 1
    planted = grant + "from l3io.wp.backup.adapters import restore_extension\n"
    assert len(_wpcli_lines(planted)) == 1


def test_new_db_password_is_registered_and_never_in_repr(
    rig: Rig, extension: _Extension, credential_sentinel: CredentialSentinel
) -> None:
    password = credential_sentinel.relocated_password
    flags = _flags(new_db_user="u", new_db_password=password)
    credential_sentinel.add_repr("RelocationFlags", flags)
    secrets = SecretRegistry()
    request = rig.request(relocation=flags)
    credential_sentinel.add_repr("RestoreRequest", request)
    restore_mod.drive_restore(request, ctx=None, secrets=secrets)
    assert secrets.redact(password) != password
    credential_sentinel.assert_clean()
