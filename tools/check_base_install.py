#!/usr/bin/env python3
"""Owner decision 76, AD-8 — what a base install of the package holds, and not.

Run with the interpreter of an environment the BUILT WHEEL was installed into
without extras (ci.yml's verify-install job, publish.yml's smoke job,
``make verify-wheel``):

    uv run --isolated --no-project --with <wheel> python tools/check_base_install.py

It reads the installed distribution's own metadata, so nothing is listed here:

* **present** — every base requirement (no extra; a marker true here counts)
  is installed at a version its specifier accepts. Since 1.0.0 that includes
  the family (``l3io-wordpress-database[encryption]``, ``l3io-wordpress-config``,
  ``l3io-age-passphrase``), and each extra a base requirement asks for
  (``database[encryption]``'s ``pyrage``) is checked the same way, nested;
* **absent** — every distribution only an extra brings is not installed:
  ``otel``'s SDK and exporter, and ``aws``'s ``l3io-wordpress-database[aws]``,
  which is ``boto3``. A distribution the base also brings is not "only an
  extra's" and is skipped; ``botocore`` arrives with ``boto3``, not on its own,
  so it is not named;
* the family's import packages, ``l3io.wp.database`` and ``l3io.wp.config``,
  import.

**Not covered:** a dependency installed but broken at import (beyond the two
family packages imported), and an extra's own correctness -- the "every
optional extra installs" steps cover that. Exit status 1 names each finding.

Only the standard library and ``packaging`` (a base dependency) are imported.
"""

from __future__ import annotations

import importlib
import importlib.metadata as md
import sys
from collections.abc import Callable, Iterable

from packaging.requirements import InvalidRequirement, Requirement

DISTRIBUTION = "l3io-wordpress-backup"
#: The family's import packages; a namespace package, so they are not derivable
#: from a distribution name (docs/adr/0012).
FAMILY_MODULES = ("l3io.wp.database", "l3io.wp.config")

Requires = Callable[[str], Iterable[str] | None]
Version = Callable[[str], str | None]
Extras = Callable[[str], Iterable[str]]


def _requirements(requires: Requires, distribution: str) -> list[Requirement]:
    parsed = []
    for line in requires(distribution) or ():
        try:
            parsed.append(Requirement(line))
        except InvalidRequirement:
            continue
    return parsed


def _gated(reqs: list[Requirement], extra: str) -> list[Requirement]:
    """The requirements ``extra`` selects ('' for the base install)."""
    out = []
    for req in reqs:
        marker = req.marker
        if extra == "":
            if marker is None or marker.evaluate({"extra": ""}):
                out.append(req)
        elif (
            marker is not None
            and marker.evaluate({"extra": extra})
            and not marker.evaluate({"extra": ""})
        ):
            out.append(req)
    return out


def _closure(requires: Requires, roots: list[tuple[str, str]]) -> list[Requirement]:
    """Every requirement reached from ``(distribution, extra)`` roots, nested."""
    seen: set[tuple[str, str]] = set()
    found: list[Requirement] = []
    todo = list(roots)
    while todo:
        dist, extra = todo.pop()
        if (dist, extra) in seen:
            continue
        seen.add((dist, extra))
        for req in _gated(_requirements(requires, dist), extra):
            found.append(req)
            todo.extend((req.name, nested) for nested in sorted(req.extras))
    return found


def findings(requires: Requires, version: Version, extras: Extras) -> list[str]:
    """What is wrong with the installed base, read from the metadata given."""
    found: list[str] = []
    if version(DISTRIBUTION) is None:
        return [f"{DISTRIBUTION} is not installed"]
    base = _closure(requires, [(DISTRIBUTION, "")])
    base_names = {r.name.lower() for r in base}
    for req in base:
        installed = version(req.name)
        if installed is None:
            found.append(f"base requirement {req} is not installed")
        elif not req.specifier.contains(installed, prereleases=True):
            found.append(f"base requirement {req}: {installed} is installed")
    provided = list(extras(DISTRIBUTION))
    if not provided:
        found.append(f"{DISTRIBUTION} provides no extra: nothing to hold absent")
    for extra in provided:
        only_extra = [
            req
            for req in _closure(requires, [(DISTRIBUTION, extra)])
            if req.name.lower() not in base_names
        ]
        for req in only_extra:
            if version(req.name) is not None:
                found.append(f"extra {extra}: {req.name} is installed in the base")
    return found


def _version(name: str) -> str | None:
    try:
        return md.version(name)
    except md.PackageNotFoundError:
        return None


def _requires(name: str) -> list[str] | None:
    try:
        return md.requires(name)
    except md.PackageNotFoundError:
        return None


def _extras(name: str) -> list[str]:
    return md.metadata(name).get_all("Provides-Extra") or []


def main() -> int:
    found = findings(_requires, _version, _extras)
    for module in FAMILY_MODULES:
        try:
            importlib.import_module(module)
        except ImportError as exc:
            found.append(f"{module} does not import: {type(exc).__name__}")
    for line in found:
        print(f"base install: {line}", file=sys.stderr)
    if found:
        return 1
    print("base install ok: every base requirement met, no extra's dependency present")
    return 0


if __name__ == "__main__":
    sys.exit(main())
