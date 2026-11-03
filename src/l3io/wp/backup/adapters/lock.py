"""AD-19 — ``FcntlSiteLock``: the Site Lock as a kernel advisory lock.

The lock for a canonical Site Root is ``<sha256 of the path>.lock`` in the
per-user state directory (``platformdirs.user_state_path("l3io-wp-backup") /
"locks"``, mode ``0700``), held with ``fcntl.flock(LOCK_EX | LOCK_NB)``. The
kernel releases an ``flock`` when its holder dies for any reason, SIGKILL
included, so a lock whose holder is gone is never live: what it leaves is a
stale holder record (``<same>.json``, ``0600``), which the next acquirer
overwrites after logging ``stale_lock_reclaimed``. PID liveness only words that
warning; the kernel lock alone decides ownership, so PID reuse cannot matter.

The lock directory is never inside the Site Root — a restore scope could
otherwise delete the lock it holds — and an unusable directory is
``SiteLockLocationError`` (``NOT_READY``): a Run never proceeds unlocked. It is
injected through the constructor for tests, never read from the environment
here.

**Re-entrancy.** ``flock`` on a second descriptor conflicts even within one
process, so a per-process registry, keyed by lock file, counts depth: a second
``acquire`` by the **same** ``run_id`` increments it, the release that brings it
to zero unlocks, and an acquire by a different ``run_id`` refuses. A child made
with ``fork`` inherits the descriptor and the registry; use ``spawn``.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import os
import socket
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Final, Self

import platformdirs
import structlog

from l3io.wp.backup.domain.errors import (
    InvalidSiteDescriptorError,
    SiteLockedError,
    SiteLockLocationError,
    SiteLockReleaseError,
)
from l3io.wp.backup.domain.lock import LockHolder, utc_z

__all__ = ["APP_NAME", "FcntlLockHandle", "FcntlSiteLock", "default_lock_dir"]

#: The per-user state directory's application name, shared with Epic 3.
APP_NAME: Final = "l3io-wp-backup"
_DIR_MODE: Final = 0o700
_FILE_MODE: Final = 0o600
#: A holder record is a few hundred bytes; anything larger is not one.
_RECORD_LIMIT: Final = 64 * 1024

_log = structlog.get_logger(__name__)


def default_lock_dir() -> Path:
    """The per-user lock directory: ``<user state dir>/locks``."""
    return platformdirs.user_state_path(APP_NAME) / "locks"


@dataclass(slots=True)
class _Held:
    """One Site this process holds: its descriptor, holder and depth."""

    fd: int
    holder: LockHolder
    record_path: Path
    depth: int


_REGISTRY: dict[Path, _Held] = {}
_REGISTRY_LOCK = threading.Lock()


def _inside(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _read_record(path: Path) -> tuple[bool, LockHolder | None]:
    """Whether a holder record exists, and the holder it names if readable."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return False, None
    except OSError:
        return True, None
    try:
        with os.fdopen(fd, "rb") as handle:
            raw = handle.read(_RECORD_LIMIT + 1)
        if len(raw) > _RECORD_LIMIT:
            return True, None
        record = json.loads(raw.decode("utf-8"))
        if not isinstance(record, dict):
            return True, None
        return True, LockHolder.from_record(record)
    except (OSError, UnicodeDecodeError, ValueError, TypeError):
        return True, None


def _write_record(path: Path, holder: LockHolder, site_root: Path) -> None:
    """Replace the holder record atomically, ``0600``, with ``json.dumps``."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(holder.as_record(str(site_root)), sort_keys=True))
            handle.flush()
            os.fsync(handle.fileno())
        Path(tmp).replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            Path(tmp).unlink()
        raise


def _pid_alive(pid: int) -> bool:
    """Words the stale-lock warning only; never decides ownership."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _holder_fields(holder: LockHolder | None) -> dict[str, object]:
    if holder is None:
        return {"holder_run_id": None, "holder_pid": None, "holder_started_at": None}
    return {
        "holder_run_id": holder.run_id,
        "holder_pid": holder.pid,
        "holder_started_at": utc_z(holder.started_at),
        "holder_hostname": holder.hostname,
    }


def _refusal(
    site_root: Path, lock_path: Path, holder: LockHolder | None
) -> SiteLockedError:
    fields = _holder_fields(holder)
    _log.error(
        "site_locked", site_root=str(site_root), lock_path=str(lock_path), **fields
    )
    if holder is None:
        return SiteLockedError(site_root, lock_path)
    return SiteLockedError(
        site_root,
        lock_path,
        holder_run_id=holder.run_id,
        holder_pid=holder.pid,
        holder_started_at=utc_z(holder.started_at),
        holder_hostname=holder.hostname,
    )


class FcntlLockHandle:
    """One acquisition of a Site Lock; the release that ends the last unlocks."""

    __slots__ = ("_lock_path", "_released", "_site_root")

    def __init__(self, site_root: Path, lock_path: Path) -> None:
        self._site_root = site_root
        self._lock_path = lock_path
        self._released = False

    @property
    def site_root(self) -> Path:
        """The canonical Site Root the lock is keyed on."""
        return self._site_root

    @property
    def lock_path(self) -> Path:
        """The lock file."""
        return self._lock_path

    def release(self) -> None:
        """Release this acquisition; :class:`SiteLockReleaseError` if repeated."""
        with _REGISTRY_LOCK:
            held = _REGISTRY.get(self._lock_path)
            if self._released or held is None:
                raise SiteLockReleaseError(self._site_root)
            self._released = True
            held.depth -= 1
            if held.depth > 0:
                return
            del _REGISTRY[self._lock_path]
            try:
                # Removed while still held: a later holder's record is never
                # deleted, and a record left behind means the holder died.
                held.record_path.unlink(missing_ok=True)
            finally:
                try:
                    fcntl.flock(held.fd, fcntl.LOCK_UN)
                finally:
                    os.close(held.fd)
        _log.debug(
            "site_lock_released",
            site_root=str(self._site_root),
            lock_path=str(self._lock_path),
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()

    def __repr__(self) -> str:
        return f"FcntlLockHandle(site_root={str(self._site_root)!r})"


class FcntlSiteLock:
    """The Site Lock over ``fcntl.flock``; ``lock_dir`` overrides the default."""

    def __init__(self, lock_dir: Path | None = None) -> None:
        self._lock_dir = lock_dir

    def _directory(self, site_root: Path) -> Path:
        """The lock directory, created ``0700``, never inside ``site_root``."""
        configured = default_lock_dir() if self._lock_dir is None else self._lock_dir
        directory = Path(configured).absolute()
        try:
            if _inside(directory.resolve(), site_root):
                reason = f"it is inside the site root {str(site_root)!r}"
                raise SiteLockLocationError(directory, reason)
            directory.mkdir(mode=_DIR_MODE, parents=True, exist_ok=True)
            # A symlink placed after the check above is caught here.
            directory = directory.resolve(strict=True)
            if _inside(directory, site_root):
                reason = f"it is inside the site root {str(site_root)!r}"
                raise SiteLockLocationError(directory, reason)
            if directory.stat().st_mode & 0o077:
                directory.chmod(_DIR_MODE)
            if not os.access(directory, os.W_OK | os.X_OK):
                raise SiteLockLocationError(directory, "it is not writable")
        except OSError as exc:
            reason = f"it cannot be created or used ({exc.strerror or exc})"
            raise SiteLockLocationError(directory, reason) from exc
        return directory

    def acquire(self, site_root: Path, holder: LockHolder) -> FcntlLockHandle:
        """Hold ``site_root``'s lock for ``holder``.

        ``site_root`` is canonicalised (``resolve(strict=True)``), so every
        spelling of one Site takes one lock. :class:`SiteLockedError` names the
        holder when another Run holds it.
        """
        try:
            canonical = Path(site_root).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            msg = "site root does not exist"
            raise InvalidSiteDescriptorError(msg, paths=(str(site_root),)) from exc
        directory = self._directory(canonical)
        digest = hashlib.sha256(str(canonical).encode()).hexdigest()
        lock_path = directory / f"{digest}.lock"
        record_path = directory / f"{digest}.json"
        with _REGISTRY_LOCK:
            held = _REGISTRY.get(lock_path)
            if held is not None:
                if held.holder.run_id != holder.run_id:
                    raise _refusal(canonical, lock_path, held.holder)
                held.depth += 1
                return FcntlLockHandle(canonical, lock_path)
            fd = self._lock(canonical, lock_path, record_path)
            _REGISTRY[lock_path] = _Held(
                fd=fd, holder=holder, record_path=record_path, depth=1
            )
            try:
                self._reclaim_and_record(canonical, lock_path, record_path, holder)
            except BaseException:
                del _REGISTRY[lock_path]
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)
                raise
        _log.info(
            "site_lock_acquired", site_root=str(canonical), lock_path=str(lock_path)
        )
        return FcntlLockHandle(canonical, lock_path)

    @staticmethod
    def _lock(site_root: Path, lock_path: Path, record_path: Path) -> int:
        """The lock file's descriptor, flocked; refuses if another holds it."""
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            fd = os.open(lock_path, flags, _FILE_MODE)
        except OSError as exc:
            reason = f"the lock file cannot be opened ({exc.strerror or exc})"
            raise SiteLockLocationError(lock_path, reason) from exc
        try:
            os.fchmod(fd, _FILE_MODE)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                _, holder = _read_record(record_path)
                raise _refusal(site_root, lock_path, holder) from None
            reason = f"the lock file cannot be locked ({exc.strerror or exc})"
            raise SiteLockLocationError(lock_path, reason) from exc
        return fd

    @staticmethod
    def _reclaim_and_record(
        site_root: Path, lock_path: Path, record_path: Path, holder: LockHolder
    ) -> None:
        """Warn about a dead holder's record, then write this holder's."""
        present, previous = _read_record(record_path)
        if present:
            fields = _holder_fields(previous)
            alive = (
                None
                if previous is None or previous.hostname != socket.gethostname()
                else _pid_alive(previous.pid)
            )
            _log.warning(
                "stale_lock_reclaimed",
                site_root=str(site_root),
                lock_path=str(lock_path),
                # ``previous_*``: ``run_id`` is the current Run's correlation key.
                previous_run_id=fields["holder_run_id"],
                previous_pid=fields["holder_pid"],
                previous_started_at=fields["holder_started_at"],
                previous_pid_alive=alive,
            )
        try:
            _write_record(record_path, holder, site_root)
        except OSError as exc:
            reason = f"the holder record cannot be written ({exc.strerror or exc})"
            raise SiteLockLocationError(record_path, reason) from exc
