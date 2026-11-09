"""Owner decision 58 — a restore started as root runs as the Site Root's owner.

Reverses decision 1 (``--allow-root``). The CLI's restore root reads what only
root may read first — the config file, the passphrase, ``--new-db-password-file``,
the target's ``wp-config.php`` for ``--archive latest`` and the Archive itself,
which it keeps open — and then calls :func:`drop_to_site_owner`, which switches
the process to the uid and gid that own the Site Root for the rest of the Run:
``setgroups([gid])``, ``setgid(gid)``, ``setuid(uid)``, in that order, then reads
the real, effective and saved ids back and checks that regaining root fails
(:class:`~l3io.wp.backup.domain.errors.PrivilegeDropError` otherwise). Restored
files are then created by the site's owner, and the extraction race a root
restore had (a symlink swapped in by a local user) has no root to win.

``HOME`` is set to the owner's home directory from the password database, so
the per-user state directory (the Site Lock, the Run Journal, the default
Safety Snapshots) is the owner's; an ``XDG_STATE_HOME`` the operator set is
kept, and must then be the owner's (decision 44's ownership check names it).
After the switch the kernel marks the process non-dumpable, so the site's own
PHP, running as the same user, cannot read this process's environment.

A Site Root owned by root has no owner to switch to: refused
(:class:`~l3io.wp.backup.domain.errors.RootRestoreRefusedError`). The library
(``api.restore``) never changes its host's user: :func:`require_not_root`
refuses a library restore running as root the same way.

``running_as_root`` is looked up on this module at every call, never imported
by name, so the test suite's root guard (``tests/conftest.py``) reaches every
caller when CI runs as root; ``tests/integration/test_root_restore.py`` runs
the real switch in a child process.
"""

from __future__ import annotations

import os
import pwd
from collections.abc import MutableMapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

import structlog

from l3io.wp.backup.domain.errors import (
    PrivilegeDropError,
    RootRestoreRefusedError,
    SiteRootNotFoundError,
)

__all__ = [
    "LIBRARY_RUNS_AS_HOST",
    "ROOT_OWNED_SITE_ROOT",
    "IdSystem",
    "SiteOwner",
    "drop_to_site_owner",
    "require_not_root",
    "running_as_root",
    "site_owner",
]

_log = structlog.get_logger(__name__)

#: Why a root-owned Site Root is refused.
ROOT_OWNED_SITE_ROOT: Final = (
    "the Site Root is owned by root, so there is no site owner to run the "
    "restore as; give the site's files to the web server's user (chown), or "
    "run the restore as that user"
)
#: Why a library restore running as root is refused.
LIBRARY_RUNS_AS_HOST: Final = (
    "the library does not switch users; run the restore as the Site Root's "
    "owner, or use the wpbackup2 command, which switches to it"
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


def require_not_root(site_root: Path) -> None:
    """The library's rule: a restore running as root is refused."""
    if running_as_root():
        raise RootRestoreRefusedError(
            site_root=str(site_root), reason=LIBRARY_RUNS_AS_HOST
        )


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


def drop_to_site_owner(
    site_root: Path,
    *,
    environ: MutableMapping[str, str],
    ids: IdSystem | None = None,
) -> SiteOwner | None:
    """Run as ``site_root``'s owner from here on; ``None`` when not root.

    ``environ`` is the process environment (``os.environ`` from the CLI
    root): its ``HOME`` becomes the owner's home directory when the password
    database has one. ``ids`` is ``os`` unless a test passes its own.
    """
    if not running_as_root():
        return None
    owner = site_owner(site_root)
    if owner.uid == 0:
        raise RootRestoreRefusedError(
            site_root=str(site_root), reason=ROOT_OWNED_SITE_ROOT
        )
    _switch(owner, ids if ids is not None else _OS)
    if owner.home:
        environ["HOME"] = owner.home
    _log.info(
        "restore_runs_as_site_owner",
        uid=owner.uid,
        gid=owner.gid,
        user=owner.user,
        home=owner.home,
    )
    return owner


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
