"""FR-12 — the values the search-replace port carries (E007-S01-003).

:class:`UrlRelocation` is what a restore asks to relocate to; Epic 7 projects
its own ``Relocation`` onto it. :class:`SearchReplaceReport` is what was (or,
from the no-op twin, would be) replaced: a Dry Run cannot know the stored URLs
being replaced, so its pairs carry :data:`UNRESOLVED_URL` as ``old`` and its
counts are ``None``, never a guessed number.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

__all__ = [
    "UNRESOLVED_URL",
    "Replacement",
    "SearchReplaceReport",
    "StoredUrls",
    "UrlRelocation",
]

#: The ``old`` of a planned :class:`Replacement` whose stored URL was not read.
UNRESOLVED_URL: Final = "<unresolved>"


@dataclass(frozen=True, slots=True)
class UrlRelocation:
    """The URLs a restored Site moves to; ``None`` keeps the stored one."""

    site_url: str | None
    home_url: str | None


@dataclass(frozen=True, slots=True)
class StoredUrls:
    """The ``siteurl`` and ``home`` options as the database holds them."""

    siteurl: str
    home: str


@dataclass(frozen=True, slots=True)
class Replacement:
    """One search-replace pair."""

    old: str
    new: str


@dataclass(frozen=True, slots=True)
class SearchReplaceReport:
    """What a relocation replaced; ``None`` counts are unknown, not zero."""

    pairs: tuple[Replacement, ...]
    replacements: int | None
    tables: tuple[str, ...] | None
    remaining: int | None
    merged: bool
