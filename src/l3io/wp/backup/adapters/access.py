"""Owner decision 87 — a path the Run's user cannot use is refused, never fixed.

wpbackup2 never changes the owner or group of anything. Before a Run opens,
the ``wpbackup2`` command asks :func:`require_access` about each path the
Run needs, as the user it will run as (after any switch to the Site Root's
owner, owner decisions 58 and 67); a path that user cannot read or write is
:class:`~l3io.wp.backup.domain.errors.PathNotAccessibleError` (``REFUSED``),
whose message names the path, the effective ids, the path's owner, group and
mode, and the operator's fix.

The answer is ``os.access`` with the effective ids, so ACLs, a read-only
mount and root's override are judged as the kernel judges them. A path that
does not exist yet is judged at its nearest existing ancestor, where it would
be made; a path to read that does not exist is not this module's to report
(the caller's own error names it).
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path
from typing import Literal

from l3io.wp.backup.domain.errors import (
    ArchiveNotFoundError,
    BackupError,
    PathNotAccessibleError,
)

__all__ = ["Access", "Kind", "archive_open_error", "refusal", "require_access"]

Access = Literal["read", "write"]
Kind = Literal["directory", "file"]


def _allowed(path: Path, mode: int) -> bool:
    if os.access in os.supports_effective_ids:
        return os.access(path, mode, effective_ids=True)
    return os.access(path, mode)


def _nearest_existing(path: Path) -> Path:
    probe = path
    while not os.path.lexists(probe) and probe != probe.parent:
        probe = probe.parent
    return probe


def _unsearchable_ancestor(path: Path) -> Path | None:
    """The first directory above ``path`` this process cannot enter, if any."""
    for parent in reversed(path.parents):
        if not os.path.isdir(parent):  # noqa: PTH112
            return None
        if not _allowed(parent, os.X_OK):
            return parent
    return None


def refusal(
    path: Path, *, role: str, access: Access, checked: Path
) -> PathNotAccessibleError:
    """The refusal for ``path``, with ``checked``'s owner, group and mode."""
    try:
        info = os.stat(checked)  # noqa: PTH116
        owner_uid: int | None = info.st_uid
        owner_gid: int | None = info.st_gid
        mode: int | None = stat.S_IMODE(info.st_mode)
    except OSError:
        owner_uid = owner_gid = mode = None
    return PathNotAccessibleError(
        path=str(path),
        role=role,
        access=access,
        uid=os.geteuid(),
        gid=os.getegid(),
        checked=str(checked),
        owner_uid=owner_uid,
        owner_gid=owner_gid,
        mode=mode,
    )


def require_access(
    path: Path, *, role: str, access: Access, kind: Kind = "directory"
) -> None:
    """Refuse ``path`` when this process cannot ``access`` it.

    A directory to read needs read and search permission; one to write needs
    write and search permission (to create in it). A file to read needs read
    permission. Raises :class:`PathNotAccessibleError`; returns ``None`` when
    the path is usable, or when a path to read does not exist.
    """
    absolute = Path(os.path.abspath(path))  # noqa: PTH100 - lexical
    blocked = _unsearchable_ancestor(absolute)
    if blocked is not None:
        raise refusal(absolute, role=role, access=access, checked=blocked)
    if access == "read":
        if not os.path.exists(absolute):  # noqa: PTH110
            return
        checked = absolute
    else:
        checked = _nearest_existing(absolute)
    if kind == "file" and checked == absolute:
        needed = os.R_OK if access == "read" else os.W_OK
    else:
        needed = (os.R_OK if access == "read" else os.W_OK) | os.X_OK
        if os.path.exists(checked) and not os.path.isdir(checked):  # noqa: PTH110, PTH112
            # A file where a directory is needed: not an ownership problem,
            # so not this refusal; the caller's own check names it.
            return
    if _allowed(checked, needed):
        return
    raise refusal(absolute, role=role, access=access, checked=checked)


def archive_open_error(path: Path, exc: OSError) -> BackupError:
    """The error for an Archive that could not be opened for reading.

    Permission denied on an Archive that exists is
    :class:`PathNotAccessibleError` (owner decision 87: name the owner and
    the fix); anything else is :class:`ArchiveNotFoundError`, as before.
    """
    if exc.errno in (errno.EACCES, errno.EPERM):
        absolute = Path(os.path.abspath(path))  # noqa: PTH100
        checked = _unsearchable_ancestor(absolute) or absolute
        if os.path.lexists(checked):
            return refusal(absolute, role="Archive", access="read", checked=checked)
    return ArchiveNotFoundError(path)
