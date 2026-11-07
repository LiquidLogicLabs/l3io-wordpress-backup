"""E003-S02-006 — Epic 7's seam: ``RESTORE_EXTENSION``, absent and present.

Absent: each ``RelocationFlags`` field set alone (scope from
``dataclasses.fields``) is ``USAGE`` saying relocation is unsupported, before
the lock; none set plans ``NO_RELOCATION``. Present: one assertion per seam
the factory fills.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from pathlib import Path

import pytest

pytest.importorskip("l3io.wp.database")

from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.adapters.wpcli import restore_extension
from l3io.wp.backup.cli import restore as restore_mod
from l3io.wp.backup.cli.parser import RELOCATION_UNAVAILABLE
from l3io.wp.backup.cli.restore import RelocationFlags
from l3io.wp.backup.core.restore import NO_RELOCATION, RestoreRelocation
from l3io.wp.backup.domain.credentials import ExplicitCredentialSpec
from l3io.wp.backup.domain.errors import UsageError
from l3io.wp.backup.domain.preflight import ToolRequirement
from l3io.wp.backup.domain.restore import RestoreMode
from tests.conftest import CredentialSentinel
from tests.restore_root_rig import Rig
from tests.unit.cli.conftest import RecordingSiteLock

FIELDS = [f.name for f in dataclasses.fields(RelocationFlags)]
_TOOL = ToolRequirement("wp-e003-fake", None, "the fake extension")


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    return Rig(tmp_path, monkeypatch)


def _flags(**set_: object) -> RelocationFlags:
    return dataclasses.replace(RelocationFlags.none(), **set_)  # type: ignore[arg-type]


@pytest.mark.parametrize("name", FIELDS)
def test_without_epic_7_each_relocation_flag_is_usage_before_the_lock(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setattr(restore_mod, "RESTORE_EXTENSION", None)
    value: object = Path("/x/wp-config.php") if name == "wp_config" else "value-e003"
    lock = RecordingSiteLock()
    with pytest.raises(UsageError, match=RELOCATION_UNAVAILABLE) as caught:
        restore_mod.drive_restore(
            rig.request(relocation=_flags(**{name: value})), ctx=None, site_lock=lock
        )
    assert name in str(caught.value)
    assert lock.events == []


def test_without_flags_the_plan_has_no_relocation(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(restore_mod, "RESTORE_EXTENSION", None)
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
def extension(monkeypatch: pytest.MonkeyPatch) -> _Extension:
    ext = _Extension()
    calls: list[RelocationFlags] = []

    def factory(flags: RelocationFlags, *, secrets: SecretRegistry) -> _Extension:
        calls.append(flags)
        return ext

    monkeypatch.setattr(restore_mod, "RESTORE_EXTENSION", factory)
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
    """Each statement naming ``adapters.wpcli``, unless it is the grant line."""
    offenders = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.stmt) or isinstance(
            node, ast.FunctionDef | ast.ClassDef | ast.Module
        ):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue  # a docstring is prose, not a reference
        text = ast.unparse(node)
        if "wpcli" not in text or isinstance(node, ast.If | ast.With | ast.Try):
            continue
        grant = isinstance(node, ast.ImportFrom) and [a.asname for a in node.names] == [
            "RESTORE_EXTENSION"
        ]
        if not grant:
            offenders.append(text)
    return offenders


def test_only_the_extension_line_may_name_adapters_wpcli() -> None:
    source = Path(inspect.getfile(restore_mod)).read_text(encoding="utf-8")
    assert _wpcli_lines(source) == []
    grant = (
        "from l3io.wp.backup.adapters.wpcli import restore_extension"
        " as RESTORE_EXTENSION"
    )
    assert [line for line in source.splitlines() if line.startswith(grant)] != []
    assert restore_mod.RESTORE_EXTENSION is restore_extension


def test_a_second_wpcli_reference_is_caught() -> None:
    grant = (
        "from l3io.wp.backup.adapters.wpcli import restore_extension"
        " as RESTORE_EXTENSION\n"
    )
    assert _wpcli_lines(grant) == []
    planted = grant + "from l3io.wp.backup.adapters import wpcli\n"
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
