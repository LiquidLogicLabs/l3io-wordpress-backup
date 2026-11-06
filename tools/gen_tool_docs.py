#!/usr/bin/env python3
"""FR-27 — the README's external-tools table, generated, never hand-kept.

Writes one block into ``README.md`` between its marker comments,
``tool-requirements``: one row per entry of
``adapters/preflight.py::KNOWN_TOOLS`` (its ``purpose`` and ``install_hint``),
with the minimum and the steps that need it taken from the requirements the
package's plans can emit:

* every backup plan, ``BackupScope`` from the enum crossed with plain and
  encrypted (``core/backup.py::build_backup_plan``);
* every entry of ``adapters/preflight.py::DOCUMENTED_REQUIREMENT_SOURCES``.

Requirements are merged by ``domain/preflight.py::merge_requirements``, so the
stricter minimum wins, as it does in a Run. ``core/backup.py::DUMP_REQUIREMENT``
is not a binary this package runs: it is documented by the fixed database
client row, which states database's own floor.

``--check`` writes nothing and exits 1 with a diff when the README differs;
``tests/contracts/test_tool_docs.py`` runs it under pytest.

**What this check does NOT cover:** prose elsewhere in the README that names a
tool or a version, and requirements a plan not enumerated above can emit —
``tests/contracts/test_tool_requirements_documented.py`` is the tripwire for
those.
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from l3io.wp.backup.domain.preflight import ToolRequirement

README = Path(__file__).resolve().parent.parent / "README.md"
NAME = "tool-requirements"
BEGIN = f"<!-- BEGIN GENERATED: {NAME} (tools/gen_tool_docs.py) -->"
END = f"<!-- END GENERATED: {NAME} (tools/gen_tool_docs.py) -->"
NO_MINIMUM = "—"
DATABASE_ROW = (
    "| MySQL/MariaDB client tools | client 8.4+, MariaDB 11.4+ | the database dump "
    "and load | checked per Site by l3io-wordpress-database (`check_dump`/"
    "`check_load`), never run by this package | your distribution's MySQL or "
    "MariaDB client package |"
)


def requirements() -> tuple[ToolRequirement, ...]:
    """Every requirement the enumerated plans and registered sources emit."""
    from l3io.wp.backup.adapters.preflight import DOCUMENTED_REQUIREMENT_SOURCES
    from l3io.wp.backup.core.backup import build_backup_plan
    from l3io.wp.backup.domain.backup_scope import BackupScope
    from l3io.wp.backup.domain.site import SiteDescriptor
    from l3io.wp.backup.domain.storage_target import StorageTarget

    site = SiteDescriptor(
        site_root=PurePosixPath("/site"),
        db_host="localhost",
        db_port=3306,
        db_name="site",
        table_prefix="wp_",
        site_url=None,
        home_url=None,
        credential_source="wp-config.php",
    )
    target = StorageTarget(scheme="file", path=PurePosixPath("/a.tar.gz"))
    found: list[ToolRequirement] = []
    for scope in BackupScope:
        for encrypted in (False, True):
            plan = build_backup_plan(
                site,
                scope,
                target,
                encrypted=encrypted,
                exclusion_source="none",
                exclusion_patterns=(),
            )
            found.extend(plan.required_tools)
    for source in DOCUMENTED_REQUIREMENT_SOURCES:
        found.extend(source())
    return tuple(found)


def _table() -> str:
    from l3io.wp.backup.adapters.preflight import KNOWN_TOOLS
    from l3io.wp.backup.core.backup import DUMP_REQUIREMENT
    from l3io.wp.backup.domain.preflight import merge_requirements

    collected = requirements()
    merged = {r.name: r for r in merge_requirements(collected)}
    needed_for: dict[str, list[str]] = {}
    for requirement in collected:
        steps = needed_for.setdefault(requirement.name, [])
        if requirement.needed_for not in steps:
            steps.append(requirement.needed_for)
    rows = []
    for name, info in KNOWN_TOOLS.items():
        requirement = merged.get(name)
        minimum = (
            ".".join(str(part) for part in requirement.minimum_version)
            if requirement is not None and requirement.minimum_version is not None
            else NO_MINIMUM
        )
        steps = "; ".join(needed_for.get(name, [])) or "no plan in this build"
        rows.append(
            f"| `{name}` | {minimum} | {info.purpose} | {steps} | {info.install_hint} |"
        )
    unknown = sorted(set(merged) - set(KNOWN_TOOLS) - {DUMP_REQUIREMENT.name})
    if unknown:
        msg = f"requirements outside KNOWN_TOOLS: {', '.join(unknown)}"
        raise SystemExit(msg)
    return "\n".join(
        [
            "| Tool | Minimum | Purpose | Needed for | Install |",
            "|---|---|---|---|---|",
            *rows,
            DATABASE_ROW,
        ]
    )


def render(text: str) -> str:
    """``text`` with the generated block replaced by its current content."""
    start = text.find(BEGIN)
    stop = text.find(END)
    if start < 0 or stop < start:
        msg = f"README.md has no well-formed {NAME!r} block ({BEGIN} ... {END})"
        raise SystemExit(msg)
    return text[: start + len(BEGIN)] + f"\n{_table()}\n" + text[stop:]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail on drift")
    parser.add_argument("--readme", type=Path, default=README)
    args = parser.parse_args(argv)
    current = args.readme.read_text(encoding="utf-8")
    wanted = render(current)
    if wanted == current:
        return 0
    if args.check:
        sys.stdout.writelines(
            difflib.unified_diff(
                current.splitlines(keepends=True),
                wanted.splitlines(keepends=True),
                "README.md",
                "README.md (generated)",
            )
        )
        print("README.md is stale: run `uv run python tools/gen_tool_docs.py`")
        return 1
    args.readme.write_text(wanted, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
