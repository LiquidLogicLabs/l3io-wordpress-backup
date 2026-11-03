"""FR-5 — the exclusion policy port (decided 2026-10-27).

A pure query. Epic 4's ``ExclusionSet`` satisfies it structurally and owns its
constructors; this port defines none. ``domain.exclusion.INCLUDE_EVERYTHING``
is the policy that excludes nothing.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Protocol

from l3io.wp.backup.domain.exclusion import ExclusionDecision

__all__ = ["ExclusionPolicy"]


class ExclusionPolicy(Protocol):
    """Decides, per Site Root path, whether it goes into the Archive."""

    @property
    def source(self) -> str:
        """``"default"``, ``"custom"`` or ``"none"``."""
        ...

    @property
    def patterns(self) -> tuple[str, ...]:
        """The patterns this policy applies, as the operator would read them."""
        ...

    def decide(self, relative: PurePosixPath, *, is_dir: bool) -> ExclusionDecision:
        """The decision for ``relative``, a path under the Site Root."""
        ...
