"""AD-3 — one immutable Site Descriptor, parsed once at the edge (AG-8).

Built by ``adapters/database.py`` from a located ``wp-config.php`` (or an
Archive's extracted config member) and never mutated: there are no setters,
and :func:`dataclasses.replace` is the only way to derive a changed value.

The descriptor holds **no credential object** — ``domain/`` may not import
``ports/`` (AD-1) — only ``credential_source``, the provider's label. A field
with no source in ``wp-config.php`` is ``None``, never defaulted.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from l3io.wp.backup.domain.database import DatabaseLocation
from l3io.wp.backup.domain.errors import InvalidSiteDescriptorError

__all__ = ["SiteDescriptor"]

_MAX_PORT = 65535


@dataclass(frozen=True, slots=True)
class SiteDescriptor:
    """Everything the Run knows about one Site, validated at construction."""

    site_root: PurePosixPath
    db_host: str
    db_port: int
    db_name: str
    table_prefix: str
    site_url: str | None
    home_url: str | None
    credential_source: str

    def __post_init__(self) -> None:
        if not self.site_root.is_absolute():
            msg = "site_root must be absolute"
            raise InvalidSiteDescriptorError(msg, paths=(str(self.site_root),))
        if not self.db_host:
            msg = "db_host must not be empty"
            raise InvalidSiteDescriptorError(msg)
        if not self.db_name:
            msg = "db_name must not be empty"
            raise InvalidSiteDescriptorError(msg)
        # A bool is an int to Python, and to a type checker; not a port.
        port: object = self.db_port
        if (
            isinstance(port, bool)
            or not isinstance(port, int)
            or not 1 <= port <= _MAX_PORT
        ):
            msg = f"db_port must be an integer from 1 to 65535, not {self.db_port!r}"
            raise InvalidSiteDescriptorError(msg)

    def database_location(self) -> DatabaseLocation:
        """Where this Site's database lives."""
        return DatabaseLocation(host=self.db_host, port=self.db_port, name=self.db_name)
