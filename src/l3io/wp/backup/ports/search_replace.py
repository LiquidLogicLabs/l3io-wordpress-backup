"""FR-12 — the search-replace port (E007-S01-003). Command-only, by design.

There is no ``SearchReplaceQuery``: every WP-CLI read this relocation makes
depends on a loaded Dump and a rewritten ``wp-config.php``, both produced by
commands. A "real query" in a Dry Run would read the target's **current**
database and report differently from the real Run (AD-4), so the no-op twin
reports the planned pairs with their stored URLs unresolved instead.

It takes :class:`~l3io.wp.backup.domain.search_replace.UrlRelocation`, not
Epic 7's ``Relocation``; Epic 7 projects onto it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from l3io.wp.backup.domain.search_replace import SearchReplaceReport, UrlRelocation

__all__ = ["SearchReplaceCommand"]


@runtime_checkable
class SearchReplaceCommand(Protocol):
    """What rewrites a restored Site's stored URLs."""

    def relocate_content(
        self,
        site_root: Path,
        urls: UrlRelocation,
        *,
        archive_encrypted: bool | None = None,
    ) -> SearchReplaceReport:
        """Replace the stored ``siteurl``/``home`` with ``urls`` across the content.

        ``archive_encrypted`` is whether the restored Archive was encrypted
        (authenticated), for the record a replacer logs; ``None`` when not
        known. Passed by the caller, never shared state (BL-E007-020).
        """
        ...
