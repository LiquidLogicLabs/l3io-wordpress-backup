"""AD-13 — the preflight port: are the tools a plan needs usable? A query.

It changes nothing, so a Dry Run runs it for real. Until Epic 5 supplies the
generic ``adapters/preflight`` (``shutil.which`` and a version probe), the
roots bind ``cli.preflight.RootPreflight`` (``root_preflight``).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from l3io.wp.backup.domain.preflight import ToolRequirement, ToolStatus

__all__ = ["Preflight"]


class Preflight(Protocol):
    """Reports what it finds for each required tool."""

    def check(self, requirements: Sequence[ToolRequirement]) -> Sequence[ToolStatus]:
        """One status per requirement it could look for."""
        ...
