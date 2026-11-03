"""AD-12 — every Run owns a private workspace.

:func:`run_workspace` creates the directory with stdlib
:func:`tempfile.mkdtemp` (mode ``0700`` by construction), yields it, and
removes it with :func:`shutil.rmtree` on success **and** on failure. ``parent``
overrides only *where* it is created. The Dump lives only in here, under a
per-Run directory — never at a fixed shared path. E002-S01-004 moves the
workspace under the Run Context.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import structlog

from l3io.wp.backup.domain.errors import ArchiveWriteError

__all__ = ["WORKSPACE_PREFIX", "run_workspace"]

WORKSPACE_PREFIX = "l3io-wp-backup-"

_log = structlog.get_logger(__name__)


@contextmanager
def run_workspace(parent: Path | None) -> Iterator[Path]:
    """A private ``0700`` directory for one Run, removed when the block exits."""
    try:
        created = Path(tempfile.mkdtemp(prefix=WORKSPACE_PREFIX, dir=parent))
    except OSError as exc:
        where = parent if parent is not None else Path(tempfile.gettempdir())
        raise ArchiveWriteError(where, exc.strerror or type(exc).__name__) from exc
    _log.debug("workspace_created", path=str(created))
    try:
        yield created
    finally:
        shutil.rmtree(created, ignore_errors=True)
        if created.exists():
            _log.warning("workspace_not_removed", path=str(created))
