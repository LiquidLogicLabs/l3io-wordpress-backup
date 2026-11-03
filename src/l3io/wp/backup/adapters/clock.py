"""The real ``ports.clock.Clock``."""

from __future__ import annotations

from datetime import UTC, datetime

__all__ = ["SystemClock"]


class SystemClock:
    """The system wall clock, in UTC."""

    __slots__ = ()

    def now(self) -> datetime:
        """``datetime.now(UTC)``."""
        return datetime.now(UTC)
