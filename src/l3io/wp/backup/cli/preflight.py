"""AD-13/AG-20 — preflight at the backup root, before the first command adapter.

:func:`require_tools` asks a ``Preflight`` about every requirement of the plan
and raises the first failure ``domain.preflight.preflight_failure`` finds —
``PreflightFailedError`` (``NOT_READY``) with ``found`` = ``absent``, the
version found, ``unusable (<error>)`` or ``unusable (version unknown)``. A
Run and a Dry Run both call it, since a ``Preflight`` is a query.

:class:`DumpReadinessPreflight` is what the root binds until Epic 5 supplies
the generic ``adapters/preflight`` (decided 2026-10-28). For the ``DUMP``
requirement it consults ``DatabaseQuery.check_dump`` — the database package's
own readiness check, which owns the client-tool check (ADR-0015): a blocker
meaning the server is unreachable raises ``DatabaseUnreachableError``, one
meaning the credentials failed ``CredentialsError`` (as the real dump would),
and any other finding is reported as an unusable tool naming the codes. Any
other requirement is looked up on ``PATH`` with :func:`shutil.which`, its
version not probed — so a requirement that states a minimum version fails
preflight as ``unusable (version unknown)`` until Epic 5's adapter probes
versions, rather than letting a too-old tool through to fail mid-write.
"""

from __future__ import annotations

import shutil
from collections.abc import Sequence

import structlog

from l3io.wp.backup.adapters.database import blocked_dump_error
from l3io.wp.backup.core.backup import DUMP_REQUIREMENT
from l3io.wp.backup.domain.database import DatabaseLocation
from l3io.wp.backup.domain.preflight import (
    ToolRequirement,
    ToolStatus,
    preflight_failure,
)
from l3io.wp.backup.ports.database import DatabaseQuery
from l3io.wp.backup.ports.preflight import Preflight

__all__ = ["DumpReadinessPreflight", "require_tools"]

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


def require_tools(
    preflight: Preflight, requirements: Sequence[ToolRequirement]
) -> None:
    """Raise the first requirement ``preflight`` finds unmet; else return."""
    if not requirements:
        return
    statuses = {status.name: status for status in preflight.check(requirements)}
    for requirement in requirements:
        failure = preflight_failure(requirement, statuses.get(requirement.name))
        if failure is not None:
            raise failure
    _log.info("preflight_passed", tools=[r.name for r in requirements])
