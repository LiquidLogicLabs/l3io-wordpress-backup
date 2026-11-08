"""AD-13/AG-20 — preflight at the composition roots, before the first command adapter.

:func:`require_tools` asks a ``Preflight`` about every requirement of the plan
and raises the first failure ``domain.preflight.preflight_failure`` finds —
``PreflightFailedError`` (``NOT_READY``) with ``found`` = ``absent``, the
version found, ``unusable (<error>)`` or ``unusable (version unknown)``. A
Run and a Dry Run both call it, since a ``Preflight`` is a query. It calls
``check`` even for an empty requirement list, so the platform refusal below
reaches a files-only Run too.

:class:`RootPreflight` is what every root binds, built only by
:func:`root_preflight` (E008-S01-001). It refuses an unsupported platform
first (``PreflightFailedError(tool="platform")``), then routes: the plan's
``database-dump`` requirement (``core.backup.DUMP_REQUIREMENT``) to
:class:`DumpReadinessPreflight`, every other requirement to Epic 5's
``adapters.preflight.BinaryPreflight`` run in the Run workspace — which
locates a binary with ``adapters.preflight.locate_tool``, the lookup
``adapters/restore_extension.py`` resolves ``wp`` with, so the binary checked is the
binary run.

:class:`DumpReadinessPreflight` consults ``DatabaseQuery.check_dump`` — the
database package's own readiness check, which owns the client-tool check
(ADR-0015): a blocker meaning the server is unreachable raises
``DatabaseUnreachableError``, one meaning the credentials failed
``CredentialsError`` (as the real dump would), and any other finding is
reported as an unusable tool naming the codes. Handed any other requirement
directly it looks the name up on ``PATH`` without probing a version, so a
stated minimum fails as ``unusable (version unknown)``; ``RootPreflight``
never hands it one.
"""

from __future__ import annotations

import shutil
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Final

import structlog

from l3io.wp.backup.adapters.database import blocked_dump_error
from l3io.wp.backup.adapters.preflight import BinaryPreflight, unsupported_platform
from l3io.wp.backup.core.backup import DUMP_REQUIREMENT
from l3io.wp.backup.domain.database import DatabaseLocation
from l3io.wp.backup.domain.errors import PreflightFailedError
from l3io.wp.backup.domain.preflight import (
    ToolRequirement,
    ToolStatus,
    preflight_failure,
)
from l3io.wp.backup.ports.database import DatabaseQuery
from l3io.wp.backup.ports.preflight import Preflight

if TYPE_CHECKING:
    from l3io.wp.backup.cli.context import RunContext
    from l3io.wp.backup.domain.site import SiteDescriptor

__all__ = [
    "PLATFORM_NEEDED_FOR",
    "PROBE_DIRECTORY",
    "DumpReadinessPreflight",
    "RootPreflight",
    "binary_preflight",
    "refuse_unsupported_platform",
    "require_tools",
    "root_preflight",
]

#: What the platform refusal says the platform is needed for.
PLATFORM_NEEDED_FOR: Final = "every Run (owner-only files, AD-7/AD-12)"

_log = structlog.get_logger(__name__)


class DumpReadinessPreflight:
    """``Preflight``: dump readiness through ``check_dump``; others on ``PATH``."""

    __slots__ = ("_credential_source", "_database", "_location")

    def __init__(
        self,
        database: DatabaseQuery,
        location: DatabaseLocation,
        *,
        credential_source: str,
    ) -> None:
        self._database = database
        self._location = location
        self._credential_source = credential_source

    def __repr__(self) -> str:
        return f"DumpReadinessPreflight({type(self._database).__name__})"

    def check(self, requirements: Sequence[ToolRequirement]) -> Sequence[ToolStatus]:
        """One status per requirement, in order."""
        return [self._status(requirement) for requirement in requirements]

    def _status(self, requirement: ToolRequirement) -> ToolStatus:
        if requirement.name != DUMP_REQUIREMENT.name:
            path = shutil.which(requirement.name)
            return ToolStatus(
                name=requirement.name,
                present=path is not None,
                version=None,
                path=path,
            )
        readiness = self._database.check_dump(self._location)
        if readiness.ready:
            return ToolStatus(
                name=requirement.name, present=True, version=None, path=None
            )
        error = blocked_dump_error(
            self._location,
            readiness.blockers,
            credential_source=self._credential_source,
        )
        if error is not None:
            raise error
        return ToolStatus(
            name=requirement.name,
            present=True,
            version=None,
            path=None,
            error=f"check_dump: {', '.join(readiness.blockers) or 'not ready'}",
        )


class RootPreflight:
    """``Preflight`` every root binds: the platform first, then each requirement.

    ``database-dump`` goes to ``dump``; every other requirement to ``binary``.
    One status per requirement, in requirement order. Built only by
    :func:`root_preflight` (``tests/contracts/test_preflight_construction.py``).
    """

    __slots__ = ("_binary", "_dump", "_platform")

    def __init__(
        self,
        *,
        dump: Preflight,
        binary: Preflight,
        platform: Callable[[], str] = lambda: sys.platform,
    ) -> None:
        self._dump = dump
        self._binary = binary
        self._platform = platform

    def __repr__(self) -> str:
        return f"RootPreflight(dump={self._dump!r}, binary={self._binary!r})"

    def check(self, requirements: Sequence[ToolRequirement]) -> Sequence[ToolStatus]:
        """Refuse an unsupported platform, then route each requirement."""
        refuse_unsupported_platform(self._platform())
        dumps = [r for r in requirements if r.name == DUMP_REQUIREMENT.name]
        others = [r for r in requirements if r.name != DUMP_REQUIREMENT.name]
        by_name = {
            status.name: status
            for status in [
                *(self._dump.check(dumps) if dumps else ()),
                *(self._binary.check(others) if others else ()),
            ]
        }
        return [by_name[r.name] for r in requirements if r.name in by_name]


def root_preflight(
    run: RunContext, site: SiteDescriptor, *, credential_source: str
) -> RootPreflight:
    """The one construction of the preflight every root binds (E008-S01-001)."""
    return RootPreflight(
        dump=DumpReadinessPreflight(
            run.adapters.database_query,
            site.database_location(),
            credential_source=credential_source,
        ),
        binary=binary_preflight(run),
    )


#: Where the tool probes run (BL-E008-002): the filesystem root, whose only
#: ancestor is itself, so no unprivileged user can plant a ``wp-cli.yml`` that
#: WP-CLI's upward search would meet. The Run workspace sits under the shared,
#: world-writable ``$TMPDIR``, where anyone could plant one in an ancestor.
PROBE_DIRECTORY: Final = Path("/")


def binary_preflight(run: RunContext, *, optional: bool = False) -> BinaryPreflight:
    """Epic 5's ``BinaryPreflight``, probing from :data:`PROBE_DIRECTORY`.

    ``run`` is kept so the one construction stays tied to a Run; the probe
    deliberately does not start in its workspace (BL-E008-002).
    ``optional=True`` is ``doctor``'s: no plan requires the tools it reports.
    """
    del run
    return BinaryPreflight(cwd=PROBE_DIRECTORY, optional=optional)


def refuse_unsupported_platform(platform: str | None = None) -> None:
    """``PreflightFailedError(tool="platform")`` on an unsupported platform.

    What :class:`RootPreflight` raises first; ``verify``, which has no plan
    and no preflight, calls it directly (one behaviour on every command).
    """
    reason = unsupported_platform(sys.platform if platform is None else platform)
    if reason is not None:
        raise PreflightFailedError(
            tool="platform", needed_for=PLATFORM_NEEDED_FOR, found=reason
        )


def require_tools(
    preflight: Preflight, requirements: Sequence[ToolRequirement]
) -> None:
    """Raise the first requirement ``preflight`` finds unmet; else return.

    ``check`` is called even with no requirements, so a check that refuses
    the platform itself (:class:`RootPreflight`) reaches every Run.
    """
    statuses = {status.name: status for status in preflight.check(requirements)}
    for requirement in requirements:
        failure = preflight_failure(requirement, statuses.get(requirement.name))
        if failure is not None:
            raise failure
    if requirements:
        _log.info("preflight_passed", tools=[r.name for r in requirements])
