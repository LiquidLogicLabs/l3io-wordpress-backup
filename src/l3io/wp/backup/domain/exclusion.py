"""FR-5 — the exclusion value types both ``core/exclusions`` and ``adapters/tar`` name.

Pure. Epic 4 owns the policy and its default patterns; this module defines only
the values that cross the ``ExclusionPolicy`` port, and :data:`INCLUDE_EVERYTHING`,
the policy that excludes nothing — what a Safety Snapshot passes, and what the
default resolves to until E008-S01-001 binds Epic 4's set.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final, Literal

__all__ = [
    "INCLUDE_EVERYTHING",
    "ExcludedEntry",
    "ExclusionDecision",
    "IncludeEverything",
]


@dataclass(frozen=True, slots=True)
class ExcludedEntry:
    """One path left out of the Archive, and the pattern that left it out."""

    path: str
    pattern: str
    is_dir: bool


@dataclass(frozen=True, slots=True)
class ExclusionDecision:
    """The policy's answer for one path."""

    excluded: bool
    pattern: str | None
    protected: bool


_INCLUDED: Final = ExclusionDecision(excluded=False, pattern=None, protected=False)


@dataclass(frozen=True, slots=True)
class IncludeEverything:
    """The policy that excludes nothing; satisfies the ``ExclusionPolicy`` port."""

    source: Literal["none"] = "none"
    patterns: tuple[str, ...] = ()

    def decide(self, relative: PurePosixPath, *, is_dir: bool) -> ExclusionDecision:
        """Every path is included."""
        del relative, is_dir
        return _INCLUDED


#: The one instance.
INCLUDE_EVERYTHING: Final = IncludeEverything()
