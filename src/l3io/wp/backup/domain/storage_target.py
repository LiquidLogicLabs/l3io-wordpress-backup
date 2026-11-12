"""AG-27 — the Storage Target grammar.

The caller splits the operator's text with stdlib ``urllib.parse.urlsplit`` in
``cli/`` (a bare path has ``scheme=""``) and hands the two parts here, so
``domain/`` imports no ``urllib`` and nothing splits a URL by hand. Only
``file:`` targets are accepted in v1; remote backends are v1.1.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Final, Literal

from l3io.wp.backup.domain.errors import UnsupportedStorageSchemeError, UsageError

__all__ = [
    "ARCHIVE_NAME_PREFIX",
    "DEFAULT_ARCHIVE_NAME_GLOBS",
    "LATEST",
    "LEGACY_ARCHIVE_NAME_PREFIX",
    "StorageTarget",
    "archive_site_name",
    "default_archive_name",
    "parse_storage_target",
    "sanitise_site_name",
    "unusable_site_name",
]

_FILE_SCHEMES = frozenset({"", "file"})

#: The default Archive name's prefix (owner decision 82, 1.1.0).
ARCHIVE_NAME_PREFIX: Final = "wpbak"
#: The prefix the default name had before 1.1.0 (``wpbackup2-<db>-<stamp>``).
LEGACY_ARCHIVE_NAME_PREFIX: Final = "wpbackup2"
#: ``fnmatch`` globs matching every default name, today's and the legacy one:
#: what a backup leaves out of the Site Root and what the stale ``.partial``
#: sweep removes. ``restore --archive latest`` matches by Manifest, not name.
DEFAULT_ARCHIVE_NAME_GLOBS: Final = tuple(
    f"{prefix}-*.tar.gz" for prefix in (ARCHIVE_NAME_PREFIX, LEGACY_ARCHIVE_NAME_PREFIX)
)
#: A run of characters a site name may not carry into a file name.
_UNSAFE_RUN: Final = re.compile(r"[^A-Za-z0-9._-]+")
#: ``restore --archive latest`` (owner decision 64): the newest Archive of
#: this Site in the Archive directory, chosen by its Manifest's ``created_at``.
LATEST: Final = "latest"


@dataclass(frozen=True, slots=True)
class StorageTarget:
    """Where an Archive is written."""

    scheme: Literal["file"]
    path: PurePosixPath


def parse_storage_target(scheme: str, path: str) -> StorageTarget:
    """Build a target from ``urlsplit``'s scheme and path parts."""
    if scheme.lower() not in _FILE_SCHEMES:
        raise UnsupportedStorageSchemeError(scheme=scheme)
    return StorageTarget(scheme="file", path=PurePosixPath(path))


def sanitise_site_name(text: str) -> str:
    """``text`` kept to ``[A-Za-z0-9._-]``; ``""`` when nothing is left.

    Each run of other characters becomes one ``-``, and leading and trailing
    ``-`` are dropped, so the result is one file name of portable characters
    (``my site`` is ``my-site``).
    """
    return _UNSAFE_RUN.sub("-", text).strip("-")


def unusable_site_name(text: str) -> str:
    """Why ``text`` cannot be the ``<site>`` part: the refusal's message."""
    return (
        f"the site name {text!r} has no character usable in an archive name "
        "(A-Z, a-z, 0-9, '.', '_', '-'); set --site-name "
        "(L3IO_WP_BACKUP_SITE_NAME, or site_name in the config file)"
    )


def archive_site_name(text: str) -> str:
    """``text`` as the ``<site>`` part of the default Archive name (decision 82).

    :func:`sanitise_site_name`; nothing left is a :class:`UsageError` naming
    ``--site-name``.
    """
    safe = sanitise_site_name(text)
    if not safe:
        raise UsageError(unusable_site_name(text), arguments=("site_name",))
    return safe


def default_archive_name(site: str, now: datetime) -> str:
    """``wpbak-<site>-<YYYYMMDDTHHMMSSZ>.tar.gz``, in UTC (owner decision 82).

    ``site`` is the ``--site-name`` setting, or the database name when it is
    not set, through :func:`archive_site_name`. ``now`` is supplied by the
    caller (``domain/`` reads no clock) and must be timezone-aware, so the
    UTC conversion never guesses the local zone. Names of one site sort by
    time.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        msg = "default_archive_name needs a timezone-aware datetime"
        raise UsageError(msg, arguments=("now",))
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{ARCHIVE_NAME_PREFIX}-{archive_site_name(site)}-{stamp}.tar.gz"
