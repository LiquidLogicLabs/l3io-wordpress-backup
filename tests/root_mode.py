"""Root and the restore's switch to the Site Root's owner, for the test suite.

Not a test module. Owner decision 58 (reversing decision 1): a restore the CLI
starts as root reads what only root may read, then switches the whole process
to the uid and gid that own the Site Root (``cli/privileges.py``). That switch
cannot be undone, so it must never run inside the pytest process, and CI's
containers run every job as root.

:func:`guard_root` is ``tests/conftest.py``'s autouse fixture body: as root,
it makes ``cli/privileges.running_as_root`` answer ``False`` for the test, so
an in-process restore runs as a non-root developer's does (no switch, no
refusal), exactly as before the switch existed. A test marked
:data:`ROOT_MARK` is left alone: it runs only as root and drives the real
switch in a child process (``tests/integration/test_root_restore.py``). As a
non-root user the guard does nothing; the product never switches.

The switch's logic (order, read-back, refusals) is unit-tested with
:class:`Kernel`, a recording :class:`~l3io.wp.backup.cli.privileges.IdSystem`,
in ``tests/unit/cli/test_privileges.py`` and ``tests/unit/cli/test_root_backup.py``,
which hold as root too.

**A child process is not guarded.** It runs as a real host would, so as root
(owner decisions 58, 67, 68):

- a ``wpbackup2`` child (``python -m wpbackup2``, or a script calling
  ``cli.main``) switches to its Site Root's owner, and refuses a root-owned
  one. The test gives the Site, and every directory the Run uses, to
  :data:`SITE_OWNER` with :func:`give_to_site_owner`;
- a library child (a script calling ``api.backup``) is refused as root, so it
  drops to :data:`SITE_OWNER` first with :func:`become_site_owner`, as
  decision 68 tells a host to;
- a check the test itself then makes on the Run's state directory (a Site
  Lock acquired in pytest's process) runs :func:`as_site_owner`: the state
  directory is the owner's, and the product refuses one another user owns
  (owner decision 44).

Not root, all three do nothing. :data:`SITE_OWNER` is the one owner every
root test uses; the interpreter and the virtual environment must be readable
by it (``ci.yml`` installs Python outside root's home for that), or the
switched child is refused with ``InstallationNotReadableError``.
"""

from __future__ import annotations

import os
import stat
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Final

import pytest

#: The marker of a test that needs real root and runs the switch for real.
ROOT_MARK = "root"

#: The non-root uid and gid a test's Site is given when the suite runs as
#: root: ``nobody`` on Debian and Ubuntu (CI's images). Any uid without
#: root's rights does; every root test uses this one.
SITE_OWNER: Final = 65534

#: The per-test directories a Run uses, as ``tests/conftest.py`` sets them.
RUN_DIRECTORIES: Final = (
    "XDG_STATE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "TMPDIR",
)


def running_as_root() -> bool:
    """True when this process's effective uid is 0, as the product decides it."""
    geteuid = getattr(os, "geteuid", None)
    return geteuid is not None and geteuid() == 0


def guard_root(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """As root, keep the product from switching users inside pytest."""
    if not running_as_root() or request.node.get_closest_marker(ROOT_MARK):
        return
    from l3io.wp.backup.cli import privileges

    monkeypatch.setattr(privileges, "running_as_root", lambda: False)


#: The system's temporary directory, read once at import: a test that sets
#: ``TMPDIR`` (``credential_sentinel``) must not move it.
_TEMP_ROOT: Final = Path(tempfile.gettempdir()).resolve()


def _traversable(path: Path) -> None:
    """Let others enter each ancestor of ``path`` inside the temporary directory.

    pytest makes its directories root's ``0700``; ``o+x`` lets the owner
    reach what it was given without listing anything else. Nothing outside
    the temporary directory is touched.
    """
    for parent in path.resolve().parents:
        if parent == _TEMP_ROOT or _TEMP_ROOT not in parent.parents:
            break
        mode = parent.stat().st_mode
        if not mode & stat.S_IXOTH:
            parent.chmod(stat.S_IMODE(mode) | stat.S_IXOTH)


def give_to_site_owner(*paths: Path, environ: Mapping[str, str] | None = None) -> None:
    """As root, give ``paths`` and the Run's directories to :data:`SITE_OWNER`.

    Each of ``paths`` and everything beneath it, and each directory of
    :data:`RUN_DIRECTORIES` that ``environ`` (``os.environ`` by default)
    names, become the owner's, and their ancestors in the temporary
    directory become enterable. Call it when the tree is built, just before
    starting the child: what the test writes afterwards is root's. Not root:
    nothing.
    """
    if not running_as_root():
        return
    environ = os.environ if environ is None else environ
    named = [Path(environ[k]) for k in RUN_DIRECTORIES if environ.get(k)]
    for top in (*paths, *(p for p in named if p.is_dir())):
        _traversable(top)
        for path in (top, *top.rglob("*")):
            os.chown(path, SITE_OWNER, SITE_OWNER, follow_symlinks=False)


@contextmanager
def as_site_owner() -> Iterator[None]:
    """As root, run the block with :data:`SITE_OWNER`'s effective ids.

    The supplementary groups become the owner's too (root's group would
    otherwise decide access to root's directories, never their "other"
    bits). The real uid stays root's, so root comes back afterwards: for a
    check pytest's own process makes on what a switched child left. Not
    root: nothing.
    """
    if not running_as_root():
        yield
        return
    groups = os.getgroups()
    os.setgroups([SITE_OWNER])
    os.setegid(SITE_OWNER)
    os.seteuid(SITE_OWNER)
    try:
        yield
    finally:
        os.seteuid(0)
        os.setegid(0)
        os.setgroups(groups)


def become_site_owner() -> None:
    """In a child process, as root: run as :data:`SITE_OWNER` from here on.

    What a library host does before ``api.backup`` (owner decision 68). The
    process's every id changes, so it cannot come back. Not root: nothing.
    """
    if not running_as_root():
        return
    os.setgroups([SITE_OWNER])
    os.setgid(SITE_OWNER)
    os.setuid(SITE_OWNER)


class Kernel:
    """The process's ids as the kernel keeps them, and every call made."""

    def __init__(self, *, sticky: str | None = None, regain: bool = False) -> None:
        self.uid, self.gid, self.groups = 0, 0, [0, 1, 2]
        self.calls: list[tuple[str, object]] = []
        self._sticky = sticky
        self._regain = regain

    def geteuid(self) -> int:
        return self.uid

    def setgroups(self, groups: Sequence[int], /) -> None:
        self.calls.append(("setgroups", list(groups)))
        if self._sticky != "groups":
            self.groups = list(groups)

    def setgid(self, gid: int, /) -> None:
        self.calls.append(("setgid", gid))
        if self._sticky != "gid":
            self.gid = gid

    def setuid(self, uid: int, /) -> None:
        self.calls.append(("setuid", uid))
        if self.uid != 0 and uid == 0 and not self._regain:
            raise PermissionError(1, "Operation not permitted")
        if self._sticky != "uid":
            self.uid = uid

    def getresuid(self) -> tuple[int, int, int]:
        return (self.uid, self.uid, self.uid)

    def getresgid(self) -> tuple[int, int, int]:
        return (self.gid, self.gid, self.gid)

    def getgroups(self) -> list[int]:
        return list(self.groups)


__all__ = [
    "ROOT_MARK",
    "RUN_DIRECTORIES",
    "SITE_OWNER",
    "Kernel",
    "as_site_owner",
    "become_site_owner",
    "give_to_site_owner",
    "guard_root",
    "running_as_root",
]
