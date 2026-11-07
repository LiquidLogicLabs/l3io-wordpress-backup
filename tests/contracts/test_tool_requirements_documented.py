"""FR-27 — every tool a plan can require is allow-listed and documented.

**Scope, derived.** The requirements are collected from every backup plan the
package can build — ``BackupScope`` from the enum, crossed with plain and
encrypted — plus every entry of
``adapters/preflight.py::DOCUMENTED_REQUIREMENT_SOURCES``. Each name must be on
``KNOWN_TOOLS`` and have a row in the README's generated ``tool-requirements``
table showing the same (stricter-wins) minimum. The one exemption is
``core/backup.py::DUMP_REQUIREMENT``: it is not a binary this package runs
(ADR-0015), so it maps to the fixed database-client row instead.

**The relocation tripwire.** When ``cli/restore.py::RESTORE_EXTENSION`` is set
(Epic 7's one static import), every tool its ``required_tools()`` returns — with
every relocation flag set — must be produced by a registered source, so the
table lists it. E007-S01-004 owns that registration.

**Restore plans and the passphrase backend (E008-S01-001).** Every
``RestoreMode``, read from the enum, is planned from test fixtures and its
requirements checked the same way (``DUMP_REQUIREMENT`` to the fixed row); and
what ``adapters/crypto.PassphraseCipher.required_tools()`` asks for under the
``cli`` backend must be produced by a registered source.

**What this does NOT cover.** A restore plan's ``extra_tools`` come from the
cipher and the relocation extension, both checked above; a tool a future
restore plan adds by itself is caught only through ``RestoreMode``.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path, PurePosixPath

import pytest

from l3io.wp.backup.adapters import preflight as preflight_module
from l3io.wp.backup.adapters.preflight import KNOWN_TOOLS
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.cli import restore as restore_cli
from l3io.wp.backup.cli.restore import RelocationFlags, RestoreExtensionFactory
from l3io.wp.backup.core.backup import DUMP_REQUIREMENT, build_backup_plan
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.preflight import ToolRequirement, merge_requirements
from l3io.wp.backup.domain.site import SiteDescriptor
from l3io.wp.backup.domain.storage_target import StorageTarget

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"
BEGIN = "<!-- BEGIN GENERATED: tool-requirements (tools/gen_tool_docs.py) -->"
END = "<!-- END GENERATED: tool-requirements (tools/gen_tool_docs.py) -->"
DATABASE_ROW = "| MySQL/MariaDB client tools |"
NO_MINIMUM = "—"
_ROW = re.compile(r"^\| `(?P<name>[^`]+)` \| (?P<minimum>[^|]+?) \|", re.MULTILINE)

SITE = SiteDescriptor(
    site_root=PurePosixPath("/srv/site"),
    db_host="localhost",
    db_port=3306,
    db_name="wptest",
    table_prefix="wp_",
    site_url=None,
    home_url=None,
    credential_source="wp-config.php",
)
TARGET = StorageTarget(scheme="file", path=PurePosixPath("/backups/a.tar.gz"))


def plan_requirements(
    extra_tools: tuple[ToolRequirement, ...] = (),
) -> list[ToolRequirement]:
    """``required_tools`` of every backup plan, over the enum."""
    found = []
    for scope in BackupScope:
        for encrypted in (False, True):
            plan = build_backup_plan(
                SITE,
                scope,
                TARGET,
                encrypted=encrypted,
                exclusion_source="none",
                exclusion_patterns=(),
                extra_tools=extra_tools,
            )
            found.extend(plan.required_tools)
    return found


def registered() -> list[ToolRequirement]:
    """Whatever ``DOCUMENTED_REQUIREMENT_SOURCES`` holds now (read at call time)."""
    return [
        r
        for source in preflight_module.DOCUMENTED_REQUIREMENT_SOURCES
        for r in source()
    ]


def readme_rows(text: str) -> dict[str, str]:
    start, stop = text.find(BEGIN), text.find(END)
    assert 0 <= start < stop, "the README has no tool-requirements block"
    return {m["name"]: m["minimum"] for m in _ROW.finditer(text[start:stop])}


def violations(requirements: Iterable[ToolRequirement], readme: str) -> list[str]:
    rows = readme_rows(readme)
    block = readme[readme.find(BEGIN) : readme.find(END)]
    found = []
    for requirement in merge_requirements(requirements):
        if requirement.name == DUMP_REQUIREMENT.name:
            if DATABASE_ROW not in block:
                found.append(f"{requirement.name}: no database client row")
            continue
        if requirement.name not in KNOWN_TOOLS:
            found.append(f"{requirement.name}: not on KNOWN_TOOLS")
        minimum = (
            NO_MINIMUM
            if requirement.minimum_version is None
            else ".".join(map(str, requirement.minimum_version))
        )
        if requirement.name not in rows:
            found.append(f"{requirement.name}: no README row")
        elif rows[requirement.name] != minimum:
            found.append(
                f"{requirement.name}: README minimum {rows[requirement.name]!r}, "
                f"plans require {minimum!r}"
            )
    return found


def relocation_violations(
    factory: RestoreExtensionFactory | None,
    sources: Sequence[Callable[[], tuple[ToolRequirement, ...]]],
) -> list[str]:
    if factory is None:
        return []
    flags = RelocationFlags(
        new_site_url="https://new.example",
        new_home_url="https://new.example",
        new_db_host="db.example",
        new_db_name="newdb",
        new_db_user="newuser",
        new_db_password="not-a-secret",
        wp_config=Path("/srv/site/wp-config.php"),
    )
    assert len(flags.given) == len(dataclasses.fields(RelocationFlags)), (
        "a relocation field was left unset"
    )
    needed = {r.name for r in factory(flags, secrets=SecretRegistry()).required_tools()}
    documented = {r.name for source in sources for r in source()}
    return [
        f"{name}: relocation requires it but no DOCUMENTED_REQUIREMENT_SOURCES "
        "entry produces it (E007-S01-004 registers relocation's requirements in "
        "adapters/preflight.py::DOCUMENTED_REQUIREMENT_SOURCES)"
        for name in sorted(needed - documented)
    ]


# --- the real checks -----------------------------------------------------------


def test_the_scope_is_every_backup_plan() -> None:
    """Scope assertion: the enum is walked and the dump requirement is seen."""
    assert len(BackupScope) >= 3
    names = {r.name for r in plan_requirements()}
    assert DUMP_REQUIREMENT.name in names, "no plan's requirements were collected"


def test_every_requirement_is_allow_listed_and_documented() -> None:
    readme = README.read_text(encoding="utf-8")
    assert violations([*plan_requirements(), *registered()], readme) == []


def test_every_known_tool_has_a_row() -> None:
    rows = readme_rows(README.read_text(encoding="utf-8"))
    assert set(rows) == set(KNOWN_TOOLS)


def test_relocation_requirements_are_registered() -> None:
    """Skipped, visibly, while ``RESTORE_EXTENSION`` is ``None``."""
    factory = restore_cli.RESTORE_EXTENSION
    if factory is None:
        pytest.skip(
            "no relocation in this build: cli/restore.py::RESTORE_EXTENSION is None "
            "(E007-S01-004 sets it and registers its requirements)"
        )
    assert (
        relocation_violations(factory, preflight_module.DOCUMENTED_REQUIREMENT_SOURCES)
        == []
    )


def restore_requirements() -> list[ToolRequirement]:
    """Every requirement of a restore plan, over every ``RestoreMode``."""
    from l3io.wp.backup.domain.restore import RestoreMode
    from tests.restore_builders import plan as restore_plan

    found: list[ToolRequirement] = []
    for mode in RestoreMode:
        found += restore_plan(mode).required_tools
    return found


def cipher_violations(
    sources: Sequence[Callable[[], tuple[ToolRequirement, ...]]],
    monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    from l3io.wp.backup.adapters.crypto import PassphraseCipher
    from l3io.wp.backup.domain.secret import Secret

    monkeypatch.setenv("L3IO_AGE_PASSPHRASE_BACKEND", "cli")
    needed = {r.name for r in PassphraseCipher(Secret("x" * 12)).required_tools()}
    assert needed, "the cli backend requires no tool: the scope is empty"
    documented = {r.name for source in sources for r in source()}
    return sorted(needed - documented)


def test_every_restore_plan_requirement_is_documented() -> None:
    requirements = restore_requirements()
    assert DUMP_REQUIREMENT.name in {r.name for r in requirements}
    assert violations(requirements, README.read_text(encoding="utf-8")) == []


def test_the_passphrase_backend_requirements_are_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = preflight_module.DOCUMENTED_REQUIREMENT_SOURCES
    assert cipher_violations(sources, monkeypatch) == []


def test_an_unregistered_backend_requirement_trips_the_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wp_only = preflight_module.DOCUMENTED_REQUIREMENT_SOURCES[:1]
    assert cipher_violations(wp_only, monkeypatch) == ["age", "age-plugin-batchpass"]


def test_a_planted_restore_requirement_fails() -> None:
    readme = README.read_text(encoding="utf-8")
    found = violations([*restore_requirements(), ZZ_FAKE], readme)
    assert "zz-fake: no README row" in found


# --- planted violations ----------------------------------------------------------


ZZ_FAKE = ToolRequirement("zz-fake", (1, 0), "a planted step")


def test_a_planted_plan_requirement_fails() -> None:
    readme = README.read_text(encoding="utf-8")
    found = violations(plan_requirements(extra_tools=(ZZ_FAKE,)), readme)
    assert "zz-fake: not on KNOWN_TOOLS" in found
    assert "zz-fake: no README row" in found


def test_a_planted_registered_source_is_in_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attacks the scope: a requirement only a registered source emits."""
    monkeypatch.setattr(
        preflight_module,
        "DOCUMENTED_REQUIREMENT_SOURCES",
        (lambda: (ZZ_FAKE, ToolRequirement("age", (9, 9), "a planted floor")),),
    )
    readme = README.read_text(encoding="utf-8")
    found = violations([*plan_requirements(), *registered()], readme)
    assert "zz-fake: not on KNOWN_TOOLS" in found
    assert any(v.startswith("age: README minimum") for v in found), found


def test_a_dropped_database_row_fails() -> None:
    readme = README.read_text(encoding="utf-8").replace(DATABASE_ROW, "| dropped |")
    assert violations(plan_requirements(), readme) == [
        f"{DUMP_REQUIREMENT.name}: no database client row"
    ]


class _StubExtension:
    def required_tools(self) -> tuple[ToolRequirement, ...]:
        return (ToolRequirement("zz-relocation", (2, 9), "relocation"),)


def test_a_planted_relocation_extension_trips_the_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        restore_cli, "RESTORE_EXTENSION", lambda flags, *, secrets: _StubExtension()
    )
    found = relocation_violations(
        restore_cli.RESTORE_EXTENSION,
        preflight_module.DOCUMENTED_REQUIREMENT_SOURCES,
    )
    assert len(found) == 1
    assert "zz-relocation" in found[0]
    assert "DOCUMENTED_REQUIREMENT_SOURCES" in found[0]
    assert "E007-S01-004" in found[0]
    registered_stub = (lambda: (ToolRequirement("zz-relocation", (2, 9), "r"),),)
    assert relocation_violations(restore_cli.RESTORE_EXTENSION, registered_stub) == []
