"""AD-13 — the external-tool facts preflight checks before the first effect.

Pure values. They live in ``domain/`` because ``core/backup.py`` names
:class:`ToolRequirement` and ``core/`` may not import ``ports/``; the query port
over them is ``ports.preflight.Preflight``.

:func:`merge_requirements` is how a plan's own requirements and the caller's
extra ones become one tuple: de-duplicated by ``name``, the stricter minimum
winning. :func:`preflight_failure` is the one decision of whether a tool's
status fails its requirement, and the error it fails with: ``found`` is
``"absent"``, the version found, ``"unusable (<error>)"``, or
``"unusable (version unknown)"`` when a minimum is stated and no version was
found; the install hint is passed through, never folded into ``needed_for``.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from dataclasses import dataclass

from l3io.wp.backup.domain.errors import PreflightFailedError

#: ``found`` for a tool present at a version nothing could read.
UNKNOWN_VERSION = "unusable (version unknown)"

__all__ = [
    "UNKNOWN_VERSION",
    "ToolRequirement",
    "ToolStatus",
    "merge_requirements",
    "preflight_failure",
]


@dataclass(frozen=True, slots=True)
class ToolRequirement:
    """A tool a step needs, and the oldest version it accepts."""

    name: str
    minimum_version: tuple[int, ...] | None
    needed_for: str
    install_hint: str | None = None


@dataclass(frozen=True, slots=True)
class ToolStatus:
    """What preflight found for one tool.

    ``error`` is set when the tool was found but probing it failed
    (``"exit 127"``, say); ``version`` is ``None`` when it was not probed or
    could not be read — which fails any stated minimum.
    """

    name: str
    present: bool
    version: tuple[int, ...] | None
    path: str | None
    error: str | None = None


def _stricter(
    a: tuple[int, ...] | None, b: tuple[int, ...] | None
) -> tuple[int, ...] | None:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def merge_requirements(
    *groups: Iterable[ToolRequirement],
) -> tuple[ToolRequirement, ...]:
    """Every requirement once, in first-seen order; the stricter minimum wins."""
    merged: dict[str, ToolRequirement] = {}
    for group in groups:
        for requirement in group:
            seen = merged.get(requirement.name)
            if seen is None:
                merged[requirement.name] = requirement
                continue
            minimum = _stricter(seen.minimum_version, requirement.minimum_version)
            merged[requirement.name] = dataclasses.replace(
                seen, minimum_version=minimum
            )
    return tuple(merged.values())


def _version_text(version: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in version)


def preflight_failure(
    requirement: ToolRequirement, status: ToolStatus | None
) -> PreflightFailedError | None:
    """The error ``status`` fails ``requirement`` with, or ``None`` if it passes.

    No status at all is an absent tool. A stated minimum passes only a version
    known to meet it: an unknown version (not probed, or unreadable) fails it,
    since a too-old tool would otherwise fail mid-write (AD-13). A requirement
    with no minimum needs no version.
    """
    found: str | None = None
    if status is None or not status.present:
        found = "absent"
    elif status.error is not None:
        found = f"unusable ({status.error})"
    elif requirement.minimum_version is None:
        pass
    elif status.version is None:
        found = UNKNOWN_VERSION
    elif status.version < requirement.minimum_version:
        found = _version_text(status.version)
    if found is None:
        return None
    return PreflightFailedError(
        requirement.name,
        requirement.needed_for,
        found,
        install_hint=requirement.install_hint,
    )
