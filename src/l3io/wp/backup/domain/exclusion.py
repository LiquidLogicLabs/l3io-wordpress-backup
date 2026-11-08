"""FR-5 — the exclusion value types both ``core/exclusions`` and ``adapters/tar`` name.

Pure. Epic 4 owns the policy and its default patterns; this module defines only
the values that cross the ``ExclusionPolicy`` port, and :data:`INCLUDE_EVERYTHING`,
the policy that excludes nothing — what a Safety Snapshot passes, and what the
default resolves to until E008-S01-001 binds Epic 4's set.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Final

__all__ = [
    "INCLUDE_EVERYTHING",
    "ExcludedEntry",
    "ExclusionDecision",
    "ExclusionSource",
    "IncludeEverything",
]


class ExclusionSource(StrEnum):
    """Where an exclusion policy's patterns came from (the Manifest's key).

    The one type for the value (BL-E004-016): ``ExclusionSet.source``,
    ``ManifestExclusions.source`` and :class:`IncludeEverything`. A host's own
    ``ExclusionPolicy`` returns its ``str`` value; the backup converts it when
    it records the Manifest.
    """

    DEFAULT = "default"
    CUSTOM = "custom"
    NONE = "none"


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

    source: ExclusionSource = ExclusionSource.NONE
    patterns: tuple[str, ...] = ()

    def decide(self, relative: PurePosixPath, *, is_dir: bool) -> ExclusionDecision:
        """Every path is included."""
        del relative, is_dir
        return _INCLUDED


#: The one instance.
INCLUDE_EVERYTHING: Final = IncludeEverything()
