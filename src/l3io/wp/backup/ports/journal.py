"""AD-18.1 — the Run Journal port, a query and a command (ADR-0020, ADR-0021).

The real pair is ``adapters/journal.py``'s (Epic 3, E003-S02-005), which owns
the durable write sequence and the file layout. A Dry Run binds the real query
and the no-op command, so it records nothing and resolves nothing.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Protocol, runtime_checkable

from l3io.wp.backup.domain.journal import JournalHeader, JournalRecord
from l3io.wp.backup.domain.restore import RestorePhase

__all__ = ["RunJournalCommand", "RunJournalQuery"]


class RunJournalQuery(Protocol):
    """What can be read from the Run Journals without changing them."""

    def incomplete(self, site_root: PurePosixPath) -> JournalRecord | None:
        """The unresolved journal of a Run on ``site_root`` that never finished."""
        ...


@runtime_checkable
class RunJournalCommand(Protocol):
    """What writes a Run's journal."""

    def begin_run(self, header: JournalHeader) -> None:
        """Record ``header`` durably, before the Run's first effect."""
        ...

    def phase_started(self, phase: RestorePhase, target: str) -> None:
        """Record that ``phase`` began against ``target``."""
        ...

    def step_completed(self, phase: RestorePhase, step: str) -> None:
        """Record that the named ``step`` of ``phase`` completed."""
        ...

    def phase_completed(self, phase: RestorePhase) -> None:
        """Record that ``phase`` completed."""
        ...

    def finish_run(self) -> None:
        """Record that the Run finished; its journal is no longer incomplete."""
        ...

    def resolve(self, run_id: str, resolved_by: str) -> None:
        """Acknowledge the incomplete journal of ``run_id`` (ADR-0021)."""
        ...
