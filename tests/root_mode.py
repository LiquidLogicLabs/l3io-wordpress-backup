"""The restore's root opt-in, for tests that run as whatever user runs pytest.

Not a test module. Owner decision 1 (BL-E002-098, 2026-11-07) refuses a
restore running as root (``RootRestoreRefusedError``, exit 2) unless the
caller passes ``--allow-root`` / ``allow_root=True``. CI's containers run every
job as root; a developer's run is not. A test that drives a restore for any
other reason takes the opt-in from here, so it checks the same thing either way.

The opt-in follows the effective uid at call time, the same ``os.geteuid`` the
product reads, so nothing here can switch the refusal off: as a non-root user
these return nothing and the product never needed the flag.

The refusal itself is proved by ``tests/unit/cli/test_closeout_safety.py``,
which patches ``_running_as_root`` true and passes ``allow_root=False``
explicitly, so it holds as root too.
"""

from __future__ import annotations

import os

#: The CLI flag that allows a restore running as root.
ALLOW_ROOT_FLAG = "--allow-root"


def running_as_root() -> bool:
    """True when this process's effective uid is 0, as the product decides it."""
    geteuid = getattr(os, "geteuid", None)
    return geteuid is not None and geteuid() == 0


def allow_root_argv() -> list[str]:
    """``["--allow-root"]`` as root, else ``[]``: for a ``restore`` argv."""
    return [ALLOW_ROOT_FLAG] if running_as_root() else []


def allow_root_kwargs() -> dict[str, bool]:
    """``{"allow_root": True}`` as root, else ``{}``: for ``api.restore``."""
    return {"allow_root": True} if running_as_root() else {}


__all__ = [
    "ALLOW_ROOT_FLAG",
    "allow_root_argv",
    "allow_root_kwargs",
    "running_as_root",
]
