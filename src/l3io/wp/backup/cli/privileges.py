"""Owner decisions 58 and 67 — a Run started as root runs as the Site Root's owner.

Reverses decision 1 (``--allow-root``). The CLI's ``restore`` and ``backup``
roots read what only root may read first, and then switch the process to the
uid and gid that own the Site Root for the rest of the Run. A restore reads
the config file, the passphrase, ``--new-db-password-file``, the target's
``wp-config.php`` for ``--archive latest`` and the Archive itself, which it
keeps open (decision 58); a backup reads the config file, the passphrase and
its database credentials, resolved through the whole chain (decision 67).
After a backup's switch, :func:`require_writable` refuses an Archive
directory the owner cannot write. The switch is :func:`drop_to_site_owner`,
or its two halves
:func:`owner_to_run_as` (who, refusing a root-owned Site Root, before
anything is read) and :func:`switch_to` (the switch): ``setgroups([gid])``,
``setgid(gid)``, ``setuid(uid)``, in that order, then the real, effective
and saved ids are read back and regaining root must fail
(:class:`~l3io.wp.backup.domain.errors.PrivilegeDropError` otherwise).
Restored files and new Archives are then the site owner's, and the
extraction race a root restore had (a symlink swapped in by a local user)
has no root to win.

``HOME`` is set to the owner's home directory from the password database, so
the per-user state directory (the Site Lock, the Run Journal, the default
Safety Snapshots, the default Archive directory) is the owner's, and a backup
and a restore of one site share it; an ``XDG_STATE_HOME`` the operator set is
kept, and must then be the owner's (decision 44's ownership check names it).
After the switch the kernel marks the process non-dumpable, so the site's own
PHP, running as the same user, cannot read this process's environment.

A Site Root owned by root has no owner to switch to: refused
(:class:`~l3io.wp.backup.domain.errors.RootRestoreRefusedError`,
:class:`~l3io.wp.backup.domain.errors.RootBackupRefusedError`). The library
(``api.restore``, ``api.backup``) never changes its host's user:
:func:`require_not_root` refuses a library Run as root the same way (owner
decision 68). ``api.verify`` touches no Site, takes no Site Lock and leaves
nothing behind, so it neither switches nor refuses.

``running_as_root`` is looked up on this module at every call, never imported
by name, so the test suite's root guard (``tests/conftest.py``) reaches every
caller when CI runs as root; ``tests/integration/test_root_restore.py`` and
``tests/integration/test_root_backup.py`` run the real switch in a child
process.
"""

from __future__ import annotations

import os
import pwd
from collections.abc import MutableMapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, Protocol

import structlog

from l3io.wp.backup.domain.errors import (
    ArchiveDirectoryNotWritableError,
    PrivilegeDropError,
    RootBackupRefusedError,
    RootRestoreRefusedError,
    SiteRootNotFoundError,
)

__all__ = [
    "LIBRARY_RUNS_AS_HOST",
    "ROOT_OWNED_SITE_ROOT",
    "IdSystem",
    "Operation",
    "SiteOwner",
    "drop_to_site_owner",
    "owner_to_run_as",
    "require_not_root",
    "require_writable",
    "running_as_root",
    "site_owner",
    "switch_to",
]

_log = structlog.get_logger(__name__)

#: The Runs that switch (the CLI) or refuse (the library) as root.
Operation = Literal["backup", "restore"]

#: Why a root-owned Site Root is refused.
ROOT_OWNED_SITE_ROOT: Final = (
    "the Site Root is owned by root, so there is no site owner to run as; "
    "give the site's files to the web server's user (chown), or run wpbackup2 "
    "as that user"
)
#: Why a library backup or restore running as root is refused (decision 68).
LIBRARY_RUNS_AS_HOST: Final = (
    "the library does not switch users; drop privileges to the Site Root's "
    "owner first, or use the wpbackup2 command, which switches to it"
)


class IdSystem(Protocol):
    """The ``os`` functions the switch uses; a test passes a recorder."""

    def geteuid(self) -> int: ...
    def setgroups(self, groups: Sequence[int], /) -> None: ...
    def setgid(self, gid: int, /) -> None: ...
    def setuid(self, uid: int, /) -> None: ...
    def getresuid(self) -> tuple[int, int, int]: ...
    def getresgid(self) -> tuple[int, int, int]: ...
    def getgroups(self) -> list[int]: ...


@dataclass(frozen=True, slots=True)
class SiteOwner:
    """Who owns the Site Root: its uid and gid, and the uid's name and home."""

    uid: int
    gid: int
    user: str | None
    home: str | None


def running_as_root() -> bool:
    """Whether the effective uid is 0 (``False`` where there is no uid)."""
    geteuid = getattr(os, "geteuid", None)
    return geteuid is not None and geteuid() == 0


def _refusal(
    operation: Operation, site_root: Path, reason: str
) -> RootBackupRefusedError | RootRestoreRefusedError:
    if operation == "backup":
        return RootBackupRefusedError(site_root=str(site_root), reason=reason)
    return RootRestoreRefusedError(site_root=str(site_root), reason=reason)


def require_not_root(site_root: Path, *, operation: Operation) -> None:
    """The library's rule: a backup or restore running as root is refused."""
    if running_as_root():
        raise _refusal(operation, site_root, LIBRARY_RUNS_AS_HOST)


def site_owner(site_root: Path) -> SiteOwner:
    """The uid and gid owning the canonical ``site_root``, and the uid's entry."""
    try:
        canonical = Path(site_root).resolve(strict=True)
        status = canonical.stat()
    except (OSError, RuntimeError) as exc:
        raise SiteRootNotFoundError(str(Path(site_root).absolute())) from exc
    try:
        entry = pwd.getpwuid(status.st_uid)
    except KeyError:
        return SiteOwner(uid=status.st_uid, gid=status.st_gid, user=None, home=None)
    return SiteOwner(
        uid=status.st_uid, gid=status.st_gid, user=entry.pw_name, home=entry.pw_dir
    )


def _switch(owner: SiteOwner, ids: IdSystem) -> None:
    """``setgroups``, ``setgid``, ``setuid``; then prove it held."""
    uid, gid = owner.uid, owner.gid
    try:
        ids.setgroups([gid])
        ids.setgid(gid)
        ids.setuid(uid)
    except OSError as exc:
        step = f"{type(exc).__name__} while switching"
        raise PrivilegeDropError(uid=uid, gid=gid, step=step) from exc
    if ids.getresuid() != (uid, uid, uid):
        raise PrivilegeDropError(uid=uid, gid=gid, step="the user ids did not change")
    if ids.getresgid() != (gid, gid, gid):
        raise PrivilegeDropError(uid=uid, gid=gid, step="the group ids did not change")
    if sorted(set(ids.getgroups())) not in ([gid], []):
        raise PrivilegeDropError(uid=uid, gid=gid, step="supplementary groups remain")
    try:
        ids.setuid(0)
    except PermissionError:
        return
    raise PrivilegeDropError(uid=uid, gid=gid, step="root could be regained")


def owner_to_run_as(site_root: Path, *, operation: Operation) -> SiteOwner | None:
    """Whom a Run started as root switches to; ``None`` when not root.

    Reads only the Site Root's ownership, so a CLI root can refuse a
    root-owned Site Root before it reads anything else.
    """
    if not running_as_root():
        return None
    owner = site_owner(site_root)
    if owner.uid == 0:
        raise _refusal(operation, site_root, ROOT_OWNED_SITE_ROOT)
    return owner


def _switched(event: str, owner: SiteOwner) -> None:
    _log.info(event, uid=owner.uid, gid=owner.gid, user=owner.user, home=owner.home)


def switch_to(
    owner: SiteOwner,
    *,
    environ: MutableMapping[str, str],
    operation: Operation,
    ids: IdSystem | None = None,
) -> SiteOwner:
    """Run as ``owner`` from here on; ``environ``'s ``HOME`` becomes theirs.

    ``environ`` is the process environment (``os.environ`` from the CLI
    root): its ``HOME`` becomes the owner's home directory when the password
    database has one. ``ids`` is ``os`` unless a test passes its own.
    """
    _switch(owner, ids if ids is not None else _OS)
    if owner.home:
        environ["HOME"] = owner.home
    if operation == "backup":
        _switched("backup_runs_as_site_owner", owner)
    else:
        _switched("restore_runs_as_site_owner", owner)
    return owner


def drop_to_site_owner(
    site_root: Path,
    *,
    environ: MutableMapping[str, str],
    operation: Operation,
    ids: IdSystem | None = None,
) -> SiteOwner | None:
    """:func:`owner_to_run_as`, then :func:`switch_to`; ``None`` when not root."""
    owner = owner_to_run_as(site_root, operation=operation)
    if owner is None:
        return None
    return switch_to(owner, environ=environ, operation=operation, ids=ids)


def require_writable(directory: Path, owner: SiteOwner) -> None:
    """Owner decision 67: the owner a backup switched to can write ``directory``.

    Called after the switch, so ``os.access`` answers for that owner. A
    directory not made yet (the per-user data directory) is checked at its
    nearest existing ancestor, where it would be made.
    """
    probe = directory.absolute()
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if probe.is_dir() and os.access(probe, os.W_OK | os.X_OK):
        return
    raise ArchiveDirectoryNotWritableError(
        directory=str(directory), uid=owner.uid, user=owner.user
    )


class _Os:
    """:class:`IdSystem` over ``os``."""

    def geteuid(self) -> int:
        return os.geteuid()

    def setgroups(self, groups: Sequence[int], /) -> None:
        os.setgroups(list(groups))

    def setgid(self, gid: int, /) -> None:
        os.setgid(gid)

    def setuid(self, uid: int, /) -> None:
        os.setuid(uid)

    def getresuid(self) -> tuple[int, int, int]:
        return os.getresuid()

    def getresgid(self) -> tuple[int, int, int]:
        return os.getresgid()

    def getgroups(self) -> list[int]:
        return os.getgroups()


_OS: Final = _Os()
