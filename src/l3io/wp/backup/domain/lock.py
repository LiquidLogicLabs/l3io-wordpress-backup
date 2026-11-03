"""AD-19 — the Site Lock's value types: who holds a Site.

:class:`LockHolder` is the record a holding Run leaves beside its lock file
(``adapters/lock.py``) so a refused Run can name it. It carries no credential
and no Site content — only the Run ID, PID, start time and hostname.
``started_at`` is rendered UTC ISO-8601 with the ``Z`` designator, the one
spelling (E002-S01-001).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

__all__ = ["LockHolder", "utc_z"]


def utc_z(moment: datetime) -> str:
    """``moment`` as UTC ISO-8601 with the ``Z`` designator."""
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class LockHolder:
    """The Run holding a Site Lock."""

    run_id: str
    pid: int
    started_at: datetime
    hostname: str

    def as_record(self, site_root: str) -> dict[str, object]:
        """The holder record's fields, with the canonical Site Root in clear."""
        return {
            "run_id": self.run_id,
            "pid": self.pid,
            "started_at": utc_z(self.started_at),
            "hostname": self.hostname,
            "site_root": site_root,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> LockHolder:
        """The holder a record names; ``TypeError``/``ValueError`` if not one."""
        run_id, pid = record.get("run_id"), record.get("pid")
        started, hostname = record.get("started_at"), record.get("hostname")
        if not (
            isinstance(run_id, str)
            and isinstance(pid, int)
            and not isinstance(pid, bool)
            and isinstance(started, str)
            and isinstance(hostname, str)
        ):
            msg = "not a Site Lock holder record"
            raise TypeError(msg)
        moment = datetime.fromisoformat(started.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            msg = "holder record started_at has no timezone"
            raise ValueError(msg)
        return cls(run_id=run_id, pid=pid, started_at=moment, hostname=hostname)
