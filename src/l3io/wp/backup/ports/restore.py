"""AD-1, AD-4, ADR-0016 — what a restore's executor reaches effects through.

:class:`RestorePorts` is declared here, once; E003-S02-003, E003-S02-005 and
E003-S02-006 cite it. It lives in ``ports/``, not ``core/``: its fields name
port types and ``core/`` may not import ``ports/``. ``adapters`` is the Run's
bound :class:`~l3io.wp.backup.ports.adapter_set.AdapterSet`, held whole
(``tests/contracts/test_adapter_set_owner.py`` refuses a second class holding
port halves), so a Dry Run is these ports over ``dry_run_adapters(...)``'s
set. :func:`restore_ports` is the one constructor; only the restore root
(E003-S02-006) calls it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from l3io.wp.backup.domain.preflight import ToolRequirement
from l3io.wp.backup.ports.adapter_set import AdapterSet

__all__ = ["RestorePorts", "restore_ports"]


@dataclass(frozen=True, slots=True)
class RestorePorts:
    """The Run's bound adapters and the three callables a restore also needs.

    ``preflight`` raises the first unmet tool requirement
    (``PreflightFailedError``); ``free_bytes(path)`` is the free bytes of the
    filesystem holding ``path`` (``shutil.disk_usage``, bound by the root);
    ``cutover_credentials`` returns the relocated WordPress account's
    password at call time, or is ``None`` without relocation.
    """

    adapters: AdapterSet
    preflight: Callable[[tuple[ToolRequirement, ...]], None]
    free_bytes: Callable[[Path], int]
    cutover_credentials: Callable[[], str] | None

    def __repr__(self) -> str:
        # The credential callable is named, never called.
        return f"RestorePorts(adapters={self.adapters!r})"


def restore_ports(
    adapters: AdapterSet,
    *,
    preflight: Callable[[tuple[ToolRequirement, ...]], None],
    free_bytes: Callable[[Path], int],
    cutover_credentials: Callable[[], str] | None,
) -> RestorePorts:
    """The one constructor of :class:`RestorePorts`; every keyword required."""
    return RestorePorts(
        adapters=adapters,
        preflight=preflight,
        free_bytes=free_bytes,
        cutover_credentials=cutover_credentials,
    )
