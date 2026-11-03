"""AD-18.2/18.4 — the values the ``wp-config.php`` port carries (ADR-0023).

Pure values only: the config file itself is read and written by the adapter
behind ``ports.config`` (E002-S02-009), which alone imports
``l3io.wp.config``, so no config library type crosses into ``core/``.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["ConfigWriteResult", "ConstantValue"]

#: A ``define()``-d constant's value as written; ``None`` is an absent one.
ConstantValue = str | int | bool | None


@dataclass(frozen=True, slots=True)
class ConfigWriteResult:
    """What a config write changed: constant **names** only, never values.

    ``performed`` is ``True`` from a real adapter and ``False`` from a no-op
    twin (ADR-0022).
    """

    changed: tuple[str, ...]
    performed: bool
