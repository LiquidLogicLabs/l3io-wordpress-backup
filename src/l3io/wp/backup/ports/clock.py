"""AD-16 — the clock port: a query, so a test can fix a Run's time.

``adapters.clock.SystemClock`` is the real one.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

__all__ = ["Clock"]


class Clock(Protocol):
    """Tells the time, and measures durations."""

    def now(self) -> datetime:
        """The current time, tz-aware UTC."""
        ...

    def monotonic_ns(self) -> int:
        """A monotonic reading in nanoseconds, for durations only (BL-E003-100)."""
        ...
