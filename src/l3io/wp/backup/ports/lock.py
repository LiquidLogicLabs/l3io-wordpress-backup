"""AD-19 — the Site Lock port: one Run per Site.

``SiteLock`` is command-only, on the single-role allowance in
``ports/__init__.py``: taking the lock is Run machinery, not a Site write, so a
Dry Run takes it for real and there is no no-op. ``adapters.lock.FcntlSiteLock``
is the real one. The composition root acquires it (``cli.context.open_run``)
before the first read of Site state and releases it in the Run's ``finally``.
"""

from __future__ import annotations

from pathlib import Path
from types import TracebackType
from typing import Protocol, Self

from l3io.wp.backup.domain.lock import LockHolder

__all__ = ["LockHandle", "SiteLock"]


class LockHandle(Protocol):
    """A held Site Lock; released once, or used as a context manager."""

    @property
    def site_root(self) -> Path:
        """The canonical Site Root the lock is keyed on."""
        ...

    def release(self) -> None:
        """Release it; ``SiteLockReleaseError`` if already released."""
        ...

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


class SiteLock(Protocol):
    """Acquires the lock for a canonical Site Root."""

    def acquire(self, site_root: Path, holder: LockHolder) -> LockHandle:
        """Hold ``site_root``'s lock for ``holder``; ``SiteLockedError`` if held."""
        ...
