"""AD-4/AD-18.2/AD-18.4 — the ``wp-config.php`` port, a query and a command (ADR-0023).

:class:`ConfigQuery` reads the **restore target's** constants (Epic 3's
``prior_constants``); it does not build a Site Descriptor, which stays
``adapters/database.py``'s (AD-3). :class:`ConfigCommand` holds the file
effects of a restore: rewriting constants, and the two of
``--overwrite-wp-config`` — saving a copy and replacing the file with the
Archive's copy, handed over as text so no intermediate file exists and a Dry
Run's no-op twin writes nothing (BL-E003-057, ADR-0023 amendment). The real
adapter is E002-S02-009's; it alone imports ``l3io.wp.config``.

``set_constants`` may receive a ``DB_PASSWORD`` value (relocation): the real
adapter registers it with the Run's ``SecretRegistry`` and the no-op records
the keys only.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

from l3io.wp.backup.domain.config import ConfigWriteResult, ConstantValue

__all__ = ["WHOLE_FILE", "ConfigCommand", "ConfigQuery"]

#: The ``key`` of a ``ConfigWriteFailedError`` about the file, not one constant.
WHOLE_FILE: Final = "(whole file)"


class ConfigQuery(Protocol):
    """What can be read from a ``wp-config.php`` without changing it."""

    def read_constants(
        self, config_file: Path, keys: Sequence[str]
    ) -> Mapping[str, ConstantValue]:
        """``keys``' values in ``config_file``; ``None`` for an undefined one."""
        ...


@runtime_checkable
class ConfigCommand(Protocol):
    """What changes a ``wp-config.php``."""

    def set_constants(
        self, config_file: Path, values: Mapping[str, str | int | bool]
    ) -> ConfigWriteResult:
        """Set ``values`` in ``config_file``; the names changed."""
        ...

    def save_copy(self, config_file: Path, destination: Path) -> ConfigWriteResult:
        """Copy ``config_file`` to ``destination`` at mode ``0600``, fsynced."""
        ...

    def replace_file(
        self,
        config_file: Path,
        content: str,
        *,
        keep: Sequence[str],
    ) -> ConfigWriteResult:
        """Replace ``config_file`` with ``content``, ``keep`` carried in first.

        ``content`` is the Archive's ``wp-config.php`` text, decoded without
        newline translation. ``keep`` names the target's constants whose
        definitions are carried into it in memory, as the source the target
        wrote (owner decision 48), before anything is written; the new file is
        written beside ``config_file``, fsynced and moved over it with
        ``os.replace``, keeping mode and owner.
        """
        ...
