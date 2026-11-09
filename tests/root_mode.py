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
"""

from __future__ import annotations

import os
from collections.abc import Sequence

import pytest

#: The marker of a test that needs real root and runs the switch for real.
ROOT_MARK = "root"


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


__all__ = ["ROOT_MARK", "Kernel", "guard_root", "running_as_root"]
